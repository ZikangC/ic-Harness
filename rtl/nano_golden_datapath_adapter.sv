// Named-tensor transport adapter for the standard Python-golden image ABI.
//
// Unlike nano_datapath_adapter, this block has no private tensor id or fixed
// descriptor offset. A controller supplies the three FNV names of a q/s/z
// matrix, or one FNV name of an int16 small tensor. Transfers are serialized
// over exactly one descriptor/DMA master pair and the resulting packed buses
// are retained until the following compute micro-op consumes them.
module nano_golden_datapath_adapter #(
  parameter int MAX_Q_BEATS = 1024,
  // The untied [64,512] head at group-size 64 has 128 scale and 128 zero
  // beats.  Keep the transport large enough for that standard-image case.
  parameter int MAX_S_BEATS = 128,
  parameter int MAX_Z_BEATS = 128,
  parameter int MAX_P_BEATS = 64
) (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic load_valid, output logic load_accept,
  input logic load_is_p16,
  input logic [31:0] q_hash, input logic [31:0] q_bytes,
  input logic [31:0] s_hash, input logic [31:0] s_bytes,
  input logic [31:0] z_hash, input logic [31:0] z_bytes,
  input logic [31:0] p_hash, input logic [31:0] p_bytes,
  output logic busy, output logic loaded, output logic error,
  output logic [MAX_Q_BEATS*128-1:0] q_bus,
  output logic [MAX_S_BEATS*128-1:0] s_bus,
  output logic [MAX_Z_BEATS*128-1:0] z_bus,
  output logic [MAX_P_BEATS*128-1:0] p_bus,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
  typedef enum logic [3:0] {IDLE, START_QSZ, WAIT_QSZ, QSZ_REARM,
                            START_P16, WAIT_P16, P16_REARM, DONE, FAILED} state_t;
  state_t state;
  logic is_p16_q;
  // The micro-op FSM only holds its name/size signals stable in its *_REQ
  // state.  A qsz transfer starts one or more cycles later, so retain the
  // complete request at acceptance rather than letting the loader observe
  // the next state's default descriptor fields.
  logic [31:0] q_hash_q, q_bytes_q, s_hash_q, s_bytes_q, z_hash_q, z_bytes_q;
  logic [31:0] p_hash_q, p_bytes_q;
  logic qsz_start, qsz_busy, qsz_loaded, qsz_error;
  logic p16_start, p16_busy, p16_loaded, p16_error;
  logic qd_v, qd_r, qd_rv, qd_rr, pd_v, pd_r, pd_rv, pd_rr;
  logic [35:0] qd_a, pd_a, qx_a, px_a;
  logic qx_v, qx_r, qx_rv, qx_rr, px_v, px_r, px_rv, px_rr;
  logic [127:0] qd_d, qx_d, pd_d, px_d;

  qsz_tensor_loader #(.MAX_Q_BEATS(MAX_Q_BEATS), .MAX_S_BEATS(MAX_S_BEATS), .MAX_Z_BEATS(MAX_Z_BEATS)) qsz (
    .clk, .rst_n, .descriptor_base, .descriptor_count, .start(qsz_start), .busy(qsz_busy), .loaded(qsz_loaded), .error(qsz_error),
    .q_name_hash(q_hash_q), .q_bytes(q_bytes_q), .s_name_hash(s_hash_q), .s_bytes(s_bytes_q),
    .z_name_hash(z_hash_q), .z_bytes(z_bytes_q), .q_bus, .s_bus, .z_bus,
    .desc_mem_req_valid(qd_v), .desc_mem_req_ready(qd_r), .desc_mem_req_addr(qd_a), .desc_mem_rsp_valid(qd_rv), .desc_mem_rsp_ready(qd_rr), .desc_mem_rsp_rdata(qd_d),
    .dma_mem_req_valid(qx_v), .dma_mem_req_ready(qx_r), .dma_mem_req_addr(qx_a), .dma_mem_rsp_valid(qx_rv), .dma_mem_rsp_ready(qx_rr), .dma_mem_rsp_rdata(qx_d));
  p16_tensor_loader #(.MAX_BEATS(MAX_P_BEATS)) p16 (
    .clk, .rst_n, .descriptor_base, .descriptor_count, .start(p16_start), .name_hash(p_hash_q), .byte_count(p_bytes_q), .busy(p16_busy), .loaded(p16_loaded), .error(p16_error), .p_bus,
    .desc_mem_req_valid(pd_v), .desc_mem_req_ready(pd_r), .desc_mem_req_addr(pd_a), .desc_mem_rsp_valid(pd_rv), .desc_mem_rsp_ready(pd_rr), .desc_mem_rsp_rdata(pd_d),
    .dma_mem_req_valid(px_v), .dma_mem_req_ready(px_r), .dma_mem_req_addr(px_a), .dma_mem_rsp_valid(px_rv), .dma_mem_rsp_ready(px_rr), .dma_mem_rsp_rdata(px_d));
  always_comb begin
    load_accept = (state == IDLE) && load_valid;
    qsz_start = (state == START_QSZ); p16_start = (state == START_P16);
    busy = !(state == IDLE || state == DONE || state == FAILED);
    loaded = (state == DONE); error = (state == FAILED);
    desc_mem_req_valid = is_p16_q ? pd_v : qd_v; desc_mem_req_addr = is_p16_q ? pd_a : qd_a;
    desc_mem_rsp_ready = is_p16_q ? pd_rr : qd_rr;
    dma_mem_req_valid = is_p16_q ? px_v : qx_v; dma_mem_req_addr = is_p16_q ? px_a : qx_a;
    dma_mem_rsp_ready = is_p16_q ? px_rr : qx_rr;
    qd_r = !is_p16_q && desc_mem_req_ready; pd_r = is_p16_q && desc_mem_req_ready;
    qd_rv = !is_p16_q && desc_mem_rsp_valid; pd_rv = is_p16_q && desc_mem_rsp_valid;
    qd_d = desc_mem_rsp_rdata; pd_d = desc_mem_rsp_rdata;
    qx_r = !is_p16_q && dma_mem_req_ready; px_r = is_p16_q && dma_mem_req_ready;
    qx_rv = !is_p16_q && dma_mem_rsp_valid; px_rv = is_p16_q && dma_mem_rsp_valid;
    qx_d = dma_mem_rsp_rdata; px_d = dma_mem_rsp_rdata;
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; is_p16_q <= 0;
      q_hash_q <= '0; q_bytes_q <= '0; s_hash_q <= '0; s_bytes_q <= '0;
      z_hash_q <= '0; z_bytes_q <= '0; p_hash_q <= '0; p_bytes_q <= '0;
    end else case (state)
      IDLE: if (load_accept) begin
        is_p16_q <= load_is_p16;
        q_hash_q <= q_hash; q_bytes_q <= q_bytes;
        s_hash_q <= s_hash; s_bytes_q <= s_bytes;
        z_hash_q <= z_hash; z_bytes_q <= z_bytes;
        p_hash_q <= p_hash; p_bytes_q <= p_bytes;
        state <= load_is_p16 ? START_P16 : START_QSZ;
      end
      START_QSZ: state <= WAIT_QSZ;
      // qsz_loaded is asserted while its internal loader is in COMPLETE.
      // Do not expose DONE yet: a following matrix request must not race the
      // previous loader's COMPLETE->IDLE transition and reuse its buffer.
      WAIT_QSZ: if (qsz_error) state <= FAILED; else if (qsz_loaded) state <= QSZ_REARM;
      QSZ_REARM: if (!qsz_loaded) state <= DONE;
      START_P16: state <= WAIT_P16;
      WAIT_P16: if (p16_error) state <= FAILED; else if (p16_loaded) state <= P16_REARM;
      P16_REARM: if (!p16_loaded) state <= DONE;
      DONE, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
