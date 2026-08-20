// One named-tensor reader for the Python-golden image ABI.
//
// The requester supplies the descriptor's FNV-1a 32-bit name hash and the
// exact unpadded tensor byte count. This engine owns descriptor_lookup plus
// dma_read16 and returns the named byte stream. It deliberately has no
// knowledge of q/s/z or model layers: a golden adapter may use it for every
// matrix section and every int16 small parameter without a private tensor-id
// encoding in the transport layer.
module descriptor_dma_engine (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic req_valid, output logic req_accept,
  input logic [31:0] req_name_hash, input logic [31:0] req_byte_count,
  input logic req_check_size,
  output logic data_valid, input logic data_ready, output logic [127:0] data,
  output logic data_last, output logic done, output logic error,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
  typedef enum logic [2:0] {IDLE, LOOKUP_START, LOOKUP_WAIT, DMA_START,
                            STREAM, COMPLETE, FAILED} state_t;
  state_t state;
  logic [31:0] name_hash_q, bytes_q;
  logic check_size_q;
  logic lookup_start, lookup_found, lookup_failed, lookup_busy;
  logic [35:0] tensor_addr;
  logic [31:0] tensor_bytes, tensor_aux;
  logic dma_start, dma_busy, dma_done;
  logic dma_data_valid, dma_data_ready, dma_data_last;
  logic [127:0] dma_data;

  descriptor_lookup lookup (
    .clk, .rst_n, .start(lookup_start), .descriptor_base, .descriptor_count,
    .wanted_name_hash(name_hash_q), .busy(lookup_busy), .found(lookup_found), .failed(lookup_failed),
    .tensor_addr, .tensor_bytes, .tensor_aux,
    .mem_req_valid(desc_mem_req_valid), .mem_req_ready(desc_mem_req_ready), .mem_req_addr(desc_mem_req_addr),
    .mem_rsp_valid(desc_mem_rsp_valid), .mem_rsp_ready(desc_mem_rsp_ready), .mem_rsp_rdata(desc_mem_rsp_rdata)
  );
  dma_read16 dma (
    .clk, .rst_n, .start(dma_start), .base_addr(tensor_addr), .byte_count(bytes_q),
    .busy(dma_busy), .done(dma_done), .mem_req_valid(dma_mem_req_valid), .mem_req_ready(dma_mem_req_ready),
    .mem_req_addr(dma_mem_req_addr), .mem_rsp_valid(dma_mem_rsp_valid), .mem_rsp_ready(dma_mem_rsp_ready),
    .mem_rsp_rdata(dma_mem_rsp_rdata), .data_valid(dma_data_valid), .data_ready(dma_data_ready),
    .data(dma_data), .data_last(dma_data_last)
  );
  always_comb begin
    req_accept = (state == IDLE) && req_valid;
    lookup_start = (state == LOOKUP_START);
    dma_start = (state == DMA_START);
    data_valid = (state == STREAM) && dma_data_valid;
    data = dma_data;
    data_last = dma_data_last;
    dma_data_ready = (state == STREAM) && data_ready;
    done = (state == COMPLETE);
    error = (state == FAILED);
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; name_hash_q <= '0; bytes_q <= '0; check_size_q <= 1'b0;
    end else case (state)
      IDLE: if (req_accept) begin
        name_hash_q <= req_name_hash; bytes_q <= req_byte_count; check_size_q <= req_check_size;
        state <= LOOKUP_START;
      end
      LOOKUP_START: state <= LOOKUP_WAIT;
      LOOKUP_WAIT: begin
        if (lookup_failed) state <= FAILED;
        else if (lookup_found) begin
          if (check_size_q && tensor_bytes != bytes_q) state <= FAILED;
          else state <= DMA_START;
        end
      end
      DMA_START: state <= STREAM;
      STREAM: if (dma_data_valid && dma_data_ready && dma_data_last) state <= COMPLETE;
      COMPLETE, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
