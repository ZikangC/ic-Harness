// Linear descriptor-table lookup. A descriptor is one 16-byte beat:
// {name_hash, byte_addr, byte_count, aux}. Linear search keeps this module
// independent of any private descriptor indexing scheme.
module descriptor_lookup (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         start,
  input  logic [35:0]  descriptor_base,
  input  logic [31:0]  descriptor_count,
  input  logic [31:0]  wanted_name_hash,
  output logic         busy,
  output logic         found,
  output logic         failed,
  output logic [35:0]  tensor_addr,
  output logic [31:0]  tensor_bytes,
  output logic [31:0]  tensor_aux,
  output logic         mem_req_valid,
  input  logic         mem_req_ready,
  output logic [35:0]  mem_req_addr,
  input  logic         mem_rsp_valid,
  output logic         mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata
);
  typedef enum logic [1:0] {IDLE, REQUEST, RESPONSE, DONE} state_t;
  state_t state;
  logic [31:0] index;

  always_comb begin
    busy = (state == REQUEST) || (state == RESPONSE);
    mem_req_valid = (state == REQUEST);
    mem_req_addr = descriptor_base + ({4'd0, index} << 4);
    mem_rsp_ready = (state == RESPONSE);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; index <= '0; found <= 1'b0; failed <= 1'b0;
      tensor_addr <= '0; tensor_bytes <= '0; tensor_aux <= '0;
    end else begin
      case (state)
        IDLE: if (start) begin
          index <= '0; found <= 1'b0; failed <= 1'b0; state <= REQUEST;
        end
        REQUEST: if (mem_req_ready) state <= RESPONSE;
        RESPONSE: if (mem_rsp_valid) begin
          if (mem_rsp_rdata[31:0] == wanted_name_hash) begin
            tensor_addr <= {4'd0, mem_rsp_rdata[63:32]};
            tensor_bytes <= mem_rsp_rdata[95:64];
            tensor_aux <= mem_rsp_rdata[127:96];
            found <= 1'b1;
            state <= DONE;
          end else if (index + 1 >= descriptor_count) begin
            failed <= 1'b1;
            state <= DONE;
          end else begin
            index <= index + 1'b1;
            state <= REQUEST;
          end
        end
        DONE: if (start) begin index <= '0; found <= 1'b0; failed <= 1'b0; state <= REQUEST; end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
