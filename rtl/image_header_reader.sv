// Reads the fixed 256-byte image header through a 16-byte read port.  The
// fields exposed here are sufficient to self-configure the independent top
// without embedding public sequence lengths or group size in RTL.
module image_header_reader (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         start,
  output logic         busy,
  output logic         done,
  output logic         error,
  output logic         mem_req_valid,
  input  logic         mem_req_ready,
  output logic [35:0]  mem_req_addr,
  input  logic         mem_rsp_valid,
  output logic         mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata,
  output logic [31:0]  n_layers,
  output logic [31:0]  d_model,
  output logic [31:0]  vocab,
  output logic [31:0]  max_seq,
  output logic [31:0]  seq_len,
  output logic [31:0]  group_size,
  output logic [31:0]  layer_type_bitmap,
  output logic [31:0]  descriptor_count,
  output logic [35:0]  descriptor_addr,
  output logic [35:0]  tokens_addr,
  output logic [35:0]  logits_addr,
  output logic [35:0]  argmax_addr,
  output logic [35:0]  status_addr,
  output logic [31:0]  logit_stride
);
  typedef enum logic [1:0] {IDLE, REQUEST, RESPONSE, FINISHED} state_t;
  state_t state;
  logic [3:0] beat;
  logic [31:0] word0, word1, word2, word3;

  always_comb begin
    mem_req_valid = (state == REQUEST);
    mem_req_addr = {32'd0, beat, 4'b0000}; // header always begins at byte 0
    mem_rsp_ready = (state == RESPONSE);
    busy = (state != IDLE) && (state != FINISHED);
    done = (state == FINISHED);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      beat <= '0;
      error <= 1'b0;
      n_layers <= '0; d_model <= '0; vocab <= '0; max_seq <= '0; seq_len <= '0;
      group_size <= '0; layer_type_bitmap <= '0; descriptor_count <= '0;
      descriptor_addr <= '0; tokens_addr <= '0; logits_addr <= '0; argmax_addr <= '0;
      status_addr <= '0; logit_stride <= '0;
    end else begin
      case (state)
        IDLE: if (start) begin beat <= '0; error <= 1'b0; state <= REQUEST; end
        REQUEST: if (mem_req_ready) state <= RESPONSE;
        RESPONSE: if (mem_rsp_valid) begin
          word0 = mem_rsp_rdata[31:0];
          word1 = mem_rsp_rdata[63:32];
          word2 = mem_rsp_rdata[95:64];
          word3 = mem_rsp_rdata[127:96];
          case (beat)
            4'd0: begin
              if (word0 != 32'h4b44_4143 || word1 != 32'd1) error <= 1'b1;
              n_layers <= word2; d_model <= word3;
            end
            4'd4: begin
              vocab <= word1; max_seq <= word2; seq_len <= word3;
            end
            4'd5: begin
              group_size <= word0; layer_type_bitmap <= word1; descriptor_count <= word2;
              descriptor_addr <= {4'd0, word3};
            end
            4'd6: begin
              tokens_addr <= {4'd0, word0}; logits_addr <= {4'd0, word1};
              argmax_addr <= {4'd0, word2}; status_addr <= {4'd0, word3};
            end
            4'd7: logit_stride <= word2; // header word 30
            default: ;
          endcase
          if (beat == 4'd15)
            state <= FINISHED;
          else begin
            beat <= beat + 1'b1;
            state <= REQUEST;
          end
        end
        FINISHED: if (start) begin beat <= '0; error <= 1'b0; state <= REQUEST; end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
