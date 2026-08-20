// Load one quantized matrix from the public Python-golden image ABI.
//
// q, s and z are separate descriptors (for example `L0.kda.q.q`,
// `L0.kda.q.s`, `L0.kda.q.z`).  This wrapper deliberately serializes the
// three transfers through one descriptor/DMA port: a decoder only needs one
// projection at a time, and this keeps descriptor transport independent from
// the compute schedule.
module qsz_tensor_loader #(
  parameter int MAX_Q_BEATS = 1024,
  parameter int MAX_S_BEATS = 64,
  parameter int MAX_Z_BEATS = 64
) (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic start, output logic busy, output logic loaded, output logic error,
  input logic [31:0] q_name_hash, input logic [31:0] q_bytes,
  input logic [31:0] s_name_hash, input logic [31:0] s_bytes,
  input logic [31:0] z_name_hash, input logic [31:0] z_bytes,
  output logic [MAX_Q_BEATS*128-1:0] q_bus,
  output logic [MAX_S_BEATS*128-1:0] s_bus,
  output logic [MAX_Z_BEATS*128-1:0] z_bus,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
  typedef enum logic [3:0] {IDLE, Q_REQ, Q_WAIT, Q_GAP, S_REQ, S_WAIT,
                            S_GAP, Z_REQ, Z_WAIT, Z_GAP, COMPLETE, FAILED} state_t;
  state_t state;
  logic [1:0] section;
  logic engine_req_valid, engine_req_accept, engine_data_valid, engine_data_ready;
  logic [127:0] engine_data;
  logic engine_done, engine_error;
  logic buffer_begin, buffer_loaded;
  logic [15:0] q_beats, s_beats, z_beats;

  // All legal tensors are nonempty.  Values are exact logical byte counts;
  // dma_read16 supplies ceil(bytes/16) 128-bit beats.
  always_comb begin
    q_beats = (q_bytes + 15) >> 4;
    s_beats = (s_bytes + 15) >> 4;
    z_beats = (z_bytes + 15) >> 4;
    engine_req_valid = (state == Q_REQ) || (state == S_REQ) || (state == Z_REQ);
    section = (state == Q_WAIT) ? 2'd0 : (state == S_WAIT) ? 2'd1 : 2'd2;
    buffer_begin = (state == IDLE) && start;
    busy = !(state == IDLE || state == COMPLETE || state == FAILED);
    loaded = (state == COMPLETE);
    error = (state == FAILED);
  end

  descriptor_dma_engine engine (
    .clk, .rst_n, .descriptor_base, .descriptor_count,
    .req_valid(engine_req_valid), .req_accept(engine_req_accept),
    .req_name_hash((state == Q_REQ) ? q_name_hash : (state == S_REQ) ? s_name_hash : z_name_hash),
    .req_byte_count((state == Q_REQ) ? q_bytes : (state == S_REQ) ? s_bytes : z_bytes),
    .req_check_size(1'b1), .data_valid(engine_data_valid), .data_ready(engine_data_ready),
    .data(engine_data), .data_last(), .done(engine_done), .error(engine_error),
    .desc_mem_req_valid, .desc_mem_req_ready, .desc_mem_req_addr,
    .desc_mem_rsp_valid, .desc_mem_rsp_ready, .desc_mem_rsp_rdata,
    .dma_mem_req_valid, .dma_mem_req_ready, .dma_mem_req_addr,
    .dma_mem_rsp_valid, .dma_mem_rsp_ready, .dma_mem_rsp_rdata
  );
  matrix_qsz_buffer #(.MAX_Q_BEATS(MAX_Q_BEATS), .MAX_S_BEATS(MAX_S_BEATS), .MAX_Z_BEATS(MAX_Z_BEATS)) buffer (
    .clk, .rst_n, .begin_load(buffer_begin),
    .expected_q_beats(q_beats), .expected_s_beats(s_beats), .expected_z_beats(z_beats),
    .dma_data_valid(engine_data_valid), .dma_data_ready(engine_data_ready), .section,
    .dma_data(engine_data), .all_loaded(buffer_loaded),
    .q_beats_loaded(), .s_beats_loaded(), .z_beats_loaded(), .q_bus, .s_bus, .z_bus
  );

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) state <= IDLE;
    else case (state)
      IDLE: if (start) state <= Q_REQ;
      Q_REQ: if (engine_req_accept) state <= Q_WAIT;
      Q_WAIT: if (engine_error) state <= FAILED; else if (engine_done) state <= Q_GAP;
      // descriptor_dma_engine returns to IDLE one clock after asserting done.
      Q_GAP: state <= S_REQ;
      S_REQ: if (engine_req_accept) state <= S_WAIT;
      S_WAIT: if (engine_error) state <= FAILED; else if (engine_done) state <= S_GAP;
      S_GAP: state <= Z_REQ;
      Z_REQ: if (engine_req_accept) state <= Z_WAIT;
      Z_WAIT: if (engine_error) state <= FAILED; else if (engine_done) state <= Z_GAP;
      Z_GAP: if (buffer_loaded) state <= COMPLETE; else state <= FAILED;
      COMPLETE, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
