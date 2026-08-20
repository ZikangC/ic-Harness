// Simple latency-elastic one-beat-at-a-time 16-byte DMA reader. It is a
// correctness baseline; issuing several requests in flight is a later
// throughput optimization and does not change the consumer interface.
module dma_read16 (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         start,
  input  logic [35:0]  base_addr,
  input  logic [31:0]  byte_count,
  output logic         busy,
  output logic         done,
  output logic         mem_req_valid,
  input  logic         mem_req_ready,
  output logic [35:0]  mem_req_addr,
  input  logic         mem_rsp_valid,
  output logic         mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata,
  output logic         data_valid,
  input  logic         data_ready,
  output logic [127:0] data,
  output logic         data_last
);
  typedef enum logic [1:0] {IDLE, REQUEST, RESPONSE, DELIVER} state_t;
  state_t state;
  logic [31:0] offset;
  logic [127:0] holding;

  always_comb begin
    busy = (state != IDLE);
    done = (state == IDLE) && (offset != 0);
    mem_req_valid = (state == REQUEST);
    mem_req_addr = base_addr + offset;
    mem_rsp_ready = (state == RESPONSE);
    data_valid = (state == DELIVER);
    data = holding;
    data_last = (offset + 16 >= byte_count);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin state <= IDLE; offset <= '0; holding <= '0; end
    else begin
      case (state)
        IDLE: if (start) begin offset <= '0; state <= REQUEST; end
        REQUEST: if (mem_req_ready) state <= RESPONSE;
        RESPONSE: if (mem_rsp_valid) begin holding <= mem_rsp_rdata; state <= DELIVER; end
        DELIVER: if (data_ready) begin
          if (offset + 16 >= byte_count) state <= IDLE;
          else begin offset <= offset + 16; state <= REQUEST; end
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
