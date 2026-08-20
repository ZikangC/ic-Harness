// Stream one named int16 small tensor from the Python-golden descriptor ABI.
// The packed bus preserves byte order: element i is p_bus[i*16 +: 16].
module p16_tensor_loader #(
  parameter int MAX_BEATS = 64
) (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic start, input logic [31:0] name_hash, input logic [31:0] byte_count,
  output logic busy, output logic loaded, output logic error,
  output logic [MAX_BEATS*128-1:0] p_bus,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
  typedef enum logic [2:0] {IDLE, REQUEST, STREAM, COMPLETE, FAILED} state_t;
  state_t state;
  logic req_valid, req_accept, data_valid, data_ready, engine_done, engine_error;
  logic [127:0] data;
  logic [15:0] beat_count, expected_beats;
  logic [127:0] storage [0:MAX_BEATS-1];
  integer i;
  always_comb begin
    expected_beats = (byte_count + 15) >> 4;
    req_valid = (state == REQUEST);
    data_ready = (state == STREAM) && (beat_count < expected_beats) && (beat_count < MAX_BEATS);
    busy = (state == REQUEST) || (state == STREAM);
    loaded = (state == COMPLETE);
    error = (state == FAILED);
    p_bus = '0;
    for (i = 0; i < MAX_BEATS; i = i + 1) p_bus[i*128 +: 128] = storage[i];
  end
  descriptor_dma_engine engine (
    .clk, .rst_n, .descriptor_base, .descriptor_count, .req_valid, .req_accept,
    .req_name_hash(name_hash), .req_byte_count(byte_count), .req_check_size(1'b1),
    .data_valid, .data_ready, .data, .data_last(), .done(engine_done), .error(engine_error),
    .desc_mem_req_valid, .desc_mem_req_ready, .desc_mem_req_addr,
    .desc_mem_rsp_valid, .desc_mem_rsp_ready, .desc_mem_rsp_rdata,
    .dma_mem_req_valid, .dma_mem_req_ready, .dma_mem_req_addr,
    .dma_mem_rsp_valid, .dma_mem_rsp_ready, .dma_mem_rsp_rdata
  );
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin state <= IDLE; beat_count <= '0; end
    else case (state)
      IDLE: if (start) begin beat_count <= '0; state <= REQUEST; end
      REQUEST: if (req_accept) state <= STREAM;
      STREAM: begin
        if (data_valid && data_ready) begin storage[beat_count] <= data; beat_count <= beat_count + 1'b1; end
        if (engine_error) state <= FAILED;
        else if (engine_done) begin
          if (beat_count == expected_beats) state <= COMPLETE;
          else state <= FAILED;
        end
      end
      COMPLETE, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
