// Memory-mapped independent nano decoder top.
//
// Assert run once.  The top reads the 256B image header, then executes the
// teacher-forced token stream and finally writes logits/argmax/status through
// one 16-byte external memory port.  All model reads traverse descriptor
// lookup -> DMA -> explicit q/s/z or small-parameter buffers.
module nano_decoder_top #(
  parameter int DATA_W = 24,
  parameter int MAX_SEQ = 64
) (
  input  logic clk,
  input  logic rst_n,
  input  logic run,
  output logic busy,
  output logic done,
  output logic error,

  output logic mem_req_valid,
  input  logic mem_req_ready,
  output logic mem_req_write,
  output logic [35:0] mem_req_addr,
  output logic [127:0] mem_req_wdata,
  output logic [15:0] mem_req_wstrb,
  input  logic mem_rsp_valid,
  output logic mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata
);
  typedef enum logic [2:0] {T_IDLE, T_HEADER_START, T_HEADER_WAIT,
                            T_EXEC_START, T_EXEC_WAIT, T_DONE, T_ERROR} top_state_t;
  top_state_t top_state;
  logic header_start, header_busy, header_done, header_error;
  logic [31:0] n_layers, d_model, vocab, max_seq_cfg, seq_len, group_size, layer_type_bitmap, descriptor_count, logit_stride;
  logic [35:0] descriptor_addr, tokens_addr, logits_addr, argmax_addr, status_addr;

  logic fabric_start, fabric_busy, fabric_run_done;
  logic token_read_valid, token_read_done;
  logic [31:0] token_index;
  logic weight_job_valid, weight_job_done;
  logic [4:0] weight_job, fabric_op_code;
  logic [31:0] weight_token_index, fabric_op_token_index;
  logic adapter_op_valid, adapter_op_done;
  logic write_row_valid, write_row_done, write_final_status;
  logic [31:0] write_token_index;

  logic token_req_done;
  logic [8:0] token_id;
  logic signed [64*16-1:0] norm_offset_bus;
  logic signed [64*4*16-1:0] kda_q_taps_bus, kda_k_taps_bus, kda_v_taps_bus;
  logic signed [8*DATA_W-1:0] moe_bias_bus;
  logic signed [DATA_W-1:0] residual_score0, residual_score1;
  logic stream_error;
  logic matrix_req_valid, matrix_req_accept, matrix_dma_valid, matrix_dma_ready, matrix_loaded;
  logic [7:0] matrix_tensor_id;
  logic [2:0] matrix_expert_index;
  logic [15:0] matrix_q_beats, matrix_s_beats, matrix_z_beats;
  logic [1:0] matrix_dma_section;
  logic [127:0] matrix_dma_data;
  logic signed [512*DATA_W-1:0] logits_q14;
  logic [8:0] argmax_index;
  logic signed [DATA_W-1:0] argmax_value;

  logic writer_busy, writer_row_done, writer_run_done;
  logic signed [512*32-1:0] logits_i32;
  integer i;

  // Header master (m0), descriptor/DMA masters (m1/m2), and output writer
  // master (m3) are deliberately visible signals before arbitration.
  logic h_req_v,h_req_r,h_rsp_v,h_rsp_r;
  logic [35:0] h_req_a;
  logic [127:0] h_rsp_d;
  logic d_req_v,d_req_r,d_rsp_v,d_rsp_r;
  logic [35:0] d_req_a;
  logic [127:0] d_rsp_d;
  logic x_req_v,x_req_r,x_rsp_v,x_rsp_r;
  logic [35:0] x_req_a;
  logic [127:0] x_rsp_d;
  logic w_req_v,w_req_r,w_rsp_v,w_rsp_r;
  logic w_req_wr;
  logic [35:0] w_req_a;
  logic [127:0] w_req_d,w_rsp_d;
  logic [15:0] w_req_s;

  image_header_reader header (
    .clk, .rst_n, .start(header_start), .busy(header_busy), .done(header_done), .error(header_error),
    .mem_req_valid(h_req_v), .mem_req_ready(h_req_r), .mem_req_addr(h_req_a),
    .mem_rsp_valid(h_rsp_v), .mem_rsp_ready(h_rsp_r), .mem_rsp_rdata(h_rsp_d),
    .n_layers, .d_model, .vocab, .max_seq(max_seq_cfg), .seq_len, .group_size, .layer_type_bitmap,
    .descriptor_count, .descriptor_addr, .tokens_addr, .logits_addr, .argmax_addr, .status_addr, .logit_stride
  );
  nano_compute_fabric fabric (
    .clk, .rst_n, .start_run(fabric_start), .seq_len,
    .busy(fabric_busy), .run_done(fabric_run_done),
    .token_read_valid, .token_index, .token_read_done,
    .weight_job_valid, .weight_job, .weight_token_index, .weight_job_done,
    .op_valid(adapter_op_valid), .op_code(fabric_op_code), .op_token_index(fabric_op_token_index), .op_done(adapter_op_done),
    .write_row_valid, .write_token_index, .write_final_status, .write_row_done
  );
  weight_stream_frontend #(.DATA_W(DATA_W)) stream (
    .clk, .rst_n, .descriptor_base(descriptor_addr), .descriptor_count, .tokens_base(tokens_addr),
    .token_req_valid(token_read_valid), .token_index, .token_req_done, .token_id,
    .small_job_valid(weight_job_valid), .small_job_op(weight_job), .small_job_done(weight_job_done),
    .norm_offset_bus, .kda_q_taps_bus, .kda_k_taps_bus, .kda_v_taps_bus, .moe_bias_bus, .residual_score0, .residual_score1,
    .matrix_req_valid, .matrix_op_code(fabric_op_code), .matrix_tensor_id, .matrix_expert_index,
    .matrix_q_beats, .matrix_s_beats, .matrix_z_beats, .matrix_req_accept,
    .matrix_dma_valid, .matrix_dma_ready, .matrix_dma_section, .matrix_dma_data, .error(stream_error),
    .desc_mem_req_valid(d_req_v), .desc_mem_req_ready(d_req_r), .desc_mem_req_addr(d_req_a),
    .desc_mem_rsp_valid(d_rsp_v), .desc_mem_rsp_ready(d_rsp_r), .desc_mem_rsp_rdata(d_rsp_d),
    .dma_mem_req_valid(x_req_v), .dma_mem_req_ready(x_req_r), .dma_mem_req_addr(x_req_a),
    .dma_mem_rsp_valid(x_rsp_v), .dma_mem_rsp_ready(x_rsp_r), .dma_mem_rsp_rdata(x_rsp_d)
  );
  nano_datapath_adapter #(.DATA_W(DATA_W), .MAX_SEQ(MAX_SEQ)) adapter (
    .clk, .rst_n, .op_valid(adapter_op_valid), .op_code(fabric_op_code), .op_token_index(fabric_op_token_index), .token_id,
    .op_done(adapter_op_done), .matrix_req_valid, .matrix_tensor_id, .matrix_expert_index,
    .matrix_q_beats, .matrix_s_beats, .matrix_z_beats, .matrix_req_accept,
    .dma_data_valid(matrix_dma_valid), .dma_data_ready(matrix_dma_ready), .dma_section(matrix_dma_section),
    .dma_data(matrix_dma_data), .matrix_loaded, .norm_offset_bus, .kda_q_taps_bus, .kda_k_taps_bus, .kda_v_taps_bus,
    .moe_bias_bus, .residual_score0, .residual_score1, .logits_bus(logits_q14), .argmax_index, .argmax_value
  );
  always_comb begin
    logits_i32 = '0;
    for (i = 0; i < 512; i = i + 1)
      logits_i32[i*32 +: 32] = {{(32-DATA_W){logits_q14[i*DATA_W+DATA_W-1]}}, logits_q14[i*DATA_W +: DATA_W]};
  end
  logit_writer writer (
    .clk, .rst_n, .start_row(write_row_valid), .write_status_after_row(write_final_status),
    .logits_base_addr(logits_addr), .argmax_base_addr(argmax_addr), .status_addr, .logit_row_stride(logit_stride),
    .token_index(write_token_index), .logits_bus(logits_i32), .argmax_id({23'd0,argmax_index}),
    .busy(writer_busy), .row_done(writer_row_done), .run_done(writer_run_done),
    .mem_req_valid(w_req_v), .mem_req_ready(w_req_r), .mem_req_write(w_req_wr), .mem_req_addr(w_req_a),
    .mem_req_wdata(w_req_d), .mem_req_wstrb(w_req_s)
  );
  assign token_read_done = token_req_done;
  assign write_row_done = writer_row_done || writer_run_done;
  // The writer performs posted writes, so no read response is expected on m3.
  assign w_rsp_r = 1'b1;

  mem_arbiter4 mem_arb (
    .clk, .rst_n,
    .m0_req_valid(h_req_v), .m0_req_write(1'b0), .m0_req_addr(h_req_a), .m0_req_wdata('0), .m0_req_wstrb('0), .m0_req_ready(h_req_r), .m0_rsp_valid(h_rsp_v), .m0_rsp_ready(h_rsp_r), .m0_rsp_rdata(h_rsp_d),
    .m1_req_valid(d_req_v), .m1_req_write(1'b0), .m1_req_addr(d_req_a), .m1_req_wdata('0), .m1_req_wstrb('0), .m1_req_ready(d_req_r), .m1_rsp_valid(d_rsp_v), .m1_rsp_ready(d_rsp_r), .m1_rsp_rdata(d_rsp_d),
    .m2_req_valid(x_req_v), .m2_req_write(1'b0), .m2_req_addr(x_req_a), .m2_req_wdata('0), .m2_req_wstrb('0), .m2_req_ready(x_req_r), .m2_rsp_valid(x_rsp_v), .m2_rsp_ready(x_rsp_r), .m2_rsp_rdata(x_rsp_d),
    .m3_req_valid(w_req_v), .m3_req_write(w_req_wr), .m3_req_addr(w_req_a), .m3_req_wdata(w_req_d), .m3_req_wstrb(w_req_s), .m3_req_ready(w_req_r), .m3_rsp_valid(w_rsp_v), .m3_rsp_ready(w_rsp_r), .m3_rsp_rdata(w_rsp_d),
    .mem_req_valid, .mem_req_ready, .mem_req_write, .mem_req_addr, .mem_req_wdata, .mem_req_wstrb,
    .mem_rsp_valid, .mem_rsp_ready, .mem_rsp_rdata
  );

  always_comb begin
    header_start = (top_state == T_HEADER_START);
    fabric_start = (top_state == T_EXEC_START);
    busy = (top_state != T_IDLE) && (top_state != T_DONE) && (top_state != T_ERROR);
    done = (top_state == T_DONE);
    error = (top_state == T_ERROR);
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) top_state <= T_IDLE;
    else case (top_state)
      T_IDLE: if (run) top_state <= T_HEADER_START;
      T_HEADER_START: top_state <= T_HEADER_WAIT;
      T_HEADER_WAIT: if (header_done) begin
        if (header_error || n_layers != 32'd2 || d_model != 32'd64 || vocab != 32'd512 || seq_len == 0 || seq_len > MAX_SEQ)
          top_state <= T_ERROR;
        else top_state <= T_EXEC_START;
      end
      T_EXEC_START: top_state <= T_EXEC_WAIT;
      T_EXEC_WAIT: if (stream_error) top_state <= T_ERROR;
                   else if (fabric_run_done) top_state <= T_DONE;
      T_DONE: top_state <= T_IDLE;
      T_ERROR: top_state <= T_IDLE;
      default: top_state <= T_IDLE;
    endcase
  end
endmodule
