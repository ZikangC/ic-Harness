// Writes one token's 512 int32 logits, its uint32 argmax, then (optionally)
// the final status word. All writes obey valid/ready and are 16-byte aligned.
module logit_writer #(
  parameter int VOCAB = 512,
  parameter int LOGIT_W = 32,
  localparam int LOGIT_BEATS = VOCAB / 4
) (
  input  logic                      clk,
  input  logic                      rst_n,
  input  logic                      start_row,
  input  logic                      write_status_after_row,
  input  logic [35:0]               logits_base_addr,
  input  logic [35:0]               argmax_base_addr,
  input  logic [35:0]               status_addr,
  input  logic [31:0]               logit_row_stride,
  input  logic [31:0]               token_index,
  input  logic signed [VOCAB*LOGIT_W-1:0] logits_bus,
  input  logic [31:0]               argmax_id,
  output logic                      busy,
  output logic                      row_done,
  output logic                      run_done,
  output logic                      mem_req_valid,
  input  logic                      mem_req_ready,
  output logic                      mem_req_write,
  output logic [35:0]               mem_req_addr,
  output logic [127:0]              mem_req_wdata,
  output logic [15:0]               mem_req_wstrb
);
  typedef enum logic [2:0] {IDLE, LOGITS, ARGMAX, STATUS, DONE} state_t;
  state_t state;
  logic [$clog2(LOGIT_BEATS)-1:0] beat;

  always_comb begin
    busy = (state != IDLE) && (state != DONE);
    row_done = (state == DONE) && !write_status_after_row;
    run_done = (state == DONE) && write_status_after_row;
    mem_req_valid = (state == LOGITS) || (state == ARGMAX) || (state == STATUS);
    mem_req_write = mem_req_valid;
    mem_req_addr = '0;
    mem_req_wdata = '0;
    mem_req_wstrb = 16'hffff;
    case (state)
      LOGITS: begin
        mem_req_addr = logits_base_addr + token_index * logit_row_stride + (beat << 4);
        mem_req_wdata = logits_bus[beat*128 +: 128];
      end
      ARGMAX: begin
        mem_req_addr = argmax_base_addr + (token_index << 2);
        mem_req_wdata[31:0] = argmax_id;
        mem_req_wstrb = 16'h000f;
      end
      STATUS: begin
        mem_req_addr = status_addr;
        mem_req_wdata[31:0] = 32'h600d_d00d;
        mem_req_wstrb = 16'h000f;
      end
      default: ;
    endcase
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin state <= IDLE; beat <= '0; end
    else begin
      case (state)
        IDLE: if (start_row) begin beat <= '0; state <= LOGITS; end
        LOGITS: if (mem_req_ready) begin
          if (beat == LOGIT_BEATS-1) state <= ARGMAX;
          else beat <= beat + 1'b1;
        end
        ARGMAX: if (mem_req_ready) begin
          if (write_status_after_row) state <= STATUS;
          else state <= DONE;
        end
        STATUS: if (mem_req_ready) state <= DONE;
        DONE: state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end
endmodule
