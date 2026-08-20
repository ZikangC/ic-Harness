// Serialized datapath adapter for the independent nano decoder.
//
// A matrix request describes exactly one q/s/z tensor.  On accept, the
// requester's DMA beats are written into matrix_qsz_buffer; all compute below
// consumes that buffer, then requests the next tensor.  This deliberately
// trades throughput for a small, inspectable first implementation: no model
// tensor is silently treated as a host-side floating-point array.
module nano_datapath_adapter #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14,
  parameter int MAX_SEQ = 64
) (
  input  logic clk,
  input  logic rst_n,

  // From nano_compute_fabric.
  input  logic op_valid,
  input  logic [4:0] op_code,
  input  logic [31:0] op_token_index,
  input  logic [8:0] token_id,
  output logic op_done,

  // Matrix descriptor/DMA front end.  tensor_id plus expert_index identifies
  // one concrete image descriptor.  A front end accepts it, then sends q/s/z
  // beats using dma_section (0=q, 1=s, 2=z).
  output logic matrix_req_valid,
  output logic [7:0] matrix_tensor_id,
  output logic [2:0] matrix_expert_index,
  output logic [15:0] matrix_q_beats,
  output logic [15:0] matrix_s_beats,
  output logic [15:0] matrix_z_beats,
  input  logic matrix_req_accept,
  input  logic dma_data_valid,
  output logic dma_data_ready,
  input  logic [1:0] dma_section,
  input  logic [127:0] dma_data,
  output logic matrix_loaded,

  // Small non-q/s/z tensors are supplied by the same descriptor/DMA front
  // end into these registers before the corresponding op starts.
  input logic signed [64*16-1:0] norm_offset_bus,
  input logic signed [64*4*16-1:0] kda_q_taps_bus,
  input logic signed [64*4*16-1:0] kda_k_taps_bus,
  input logic signed [64*4*16-1:0] kda_v_taps_bus,
  input logic signed [8*DATA_W-1:0] moe_bias_bus,
  input logic signed [DATA_W-1:0] residual_score0,
  input logic signed [DATA_W-1:0] residual_score1,

  // Committed result for logit_writer / status logic.
  output logic signed [512*DATA_W-1:0] logits_bus,
  output logic [8:0] argmax_index,
  output logic signed [DATA_W-1:0] argmax_value
);
  localparam logic [4:0]
    OP_EMBED       = 5'd0,
    OP_L0_NORM1    = 5'd1,
    OP_L0_KDA      = 5'd2,
    OP_L0_RESMIX   = 5'd3,
    OP_L0_NORM2    = 5'd4,
    OP_L0_MOE      = 5'd5,
    OP_L1_RESA     = 5'd6,
    OP_L1_NORM1    = 5'd7,
    OP_L1_MLA      = 5'd8,
    OP_L1_RESMIX   = 5'd9,
    OP_L1_NORM2    = 5'd10,
    OP_L1_MOE      = 5'd11,
    OP_OUT_RESMIX  = 5'd12,
    OP_FINAL_NORM  = 5'd13,
    OP_HEAD_ARGMAX = 5'd14;

  localparam logic [2:0] S_X0 = 3'd0, S_CUR = 3'd1, S_NORM = 3'd2,
                           S_BRANCH = 3'd3;
  localparam logic [3:0]
    MK_64X64  = 4'd0, MK_64X32 = 4'd1, MK_32X64 = 4'd2,
    MK_64X96  = 4'd3, MK_64X8  = 4'd4, MK_64X1  = 4'd5,
    MK_64X512 = 4'd6, MK_EMBED = 4'd7;
  localparam logic [7:0]
    T_EMBED=8'd0, T_KDA_Q=8'd1, T_KDA_K=8'd2, T_KDA_V=8'd3,
    T_KDA_A=8'd4, T_KDA_B=8'd5, T_KDA_O=8'd6,
    T_MLA_Q=8'd7, T_MLA_K=8'd8, T_MLA_V=8'd9, T_MLA_O=8'd10,
    T_MOE_GATE=8'd11, T_MOE_G=8'd12, T_MOE_U=8'd13, T_MOE_D=8'd14,
    T_HEAD=8'd15;

  typedef enum logic [5:0] {
    ST_IDLE, ST_EMB_REQ, ST_EMB_WAIT, ST_EMB_W0, ST_EMB_WCUR,
    ST_NORM_WRITE, ST_KQ_REQ, ST_KQ_WAIT, ST_KK_REQ, ST_KK_WAIT,
    ST_KV_REQ, ST_KV_WAIT, ST_KA_REQ, ST_KA_WAIT, ST_KB_REQ, ST_KB_WAIT,
    ST_KSTEP, ST_KO_REQ, ST_KO_WAIT, ST_KWRITE, ST_MIX_WRITE,
    ST_MQ_REQ, ST_MQ_WAIT, ST_MK_REQ, ST_MK_WAIT, ST_MV_REQ, ST_MV_WAIT,
    ST_MCACHE, ST_MSTART, ST_MWAIT, ST_MO_REQ, ST_MO_WAIT, ST_MWRITE,
    ST_MG_REQ, ST_MG_WAIT, ST_E0G_REQ, ST_E0G_WAIT, ST_E0U_REQ, ST_E0U_WAIT,
    ST_E0D_REQ, ST_E0D_WAIT, ST_E1G_REQ, ST_E1G_WAIT, ST_E1U_REQ,
    ST_E1U_WAIT, ST_E1D_REQ, ST_E1D_WAIT, ST_MOE_WRITE,
    ST_HEAD_REQ, ST_HEAD_WAIT, ST_COMPLETE
  } state_t;
  state_t state;

  logic [4:0] active_op;
  logic [31:0] active_token_index;
  logic [8:0] active_token_id;
  logic [3:0] matrix_kind, loaded_matrix_kind;
  logic buffer_begin;
  logic [15:0] q_loaded, s_loaded, z_loaded;
  logic [1024*128-1:0] q_buffer;
  logic [64*128-1:0] s_buffer, z_buffer;
  logic signed [512*DATA_W-1:0] matvec_y;

  logic bank_write_valid;
  logic [2:0] bank_write_slot, bank_read_slot0, bank_read_slot1;
  logic signed [64*DATA_W-1:0] bank_write_bus, bank_read0, bank_read1;
  logic signed [64*DATA_W-1:0] rms_y, residual_y;
  logic [FRAC_W:0] residual_w0, residual_w1;

  logic signed [64*DATA_W-1:0] q_pre, k_pre, v_pre, alpha_pre;
  logic signed [DATA_W-1:0] beta_pre;
  logic signed [64*DATA_W-1:0] q_conv, k_conv, v_conv;
  logic signed [64*DATA_W-1:0] q_silu, k_silu, v_silu;
  logic signed [32*DATA_W-1:0] q0_norm, q1_norm, k0_norm, k1_norm;
  logic signed [64*DATA_W-1:0] alpha_gate;
  logic [FRAC_W:0] beta_gate;
  logic signed [32*DATA_W-1:0] kda_out0, kda_out1;
  logic signed [64*DATA_W-1:0] kda_step_result, kda_projected;

  logic signed [96*DATA_W-1:0] mla_q, mla_k;
  logic signed [64*DATA_W-1:0] mla_v, mla_context, mla_projected;
  logic [5:0] mla_cache_index;
  logic signed [2*48*DATA_W-1:0] cache_key_bus;
  logic signed [2*32*DATA_W-1:0] cache_value_bus;
  logic mla_head0_busy, mla_head1_busy, mla_head0_done, mla_head1_done;
  logic signed [32*DATA_W-1:0] mla_ctx0, mla_ctx1;

  logic signed [8*DATA_W-1:0] moe_gate_pre, moe_gate_scores;
  logic [2:0] moe_expert0, moe_expert1;
  logic signed [DATA_W-1:0] moe_weight0, moe_weight1;
  logic signed [32*DATA_W-1:0] e0_gate, e0_up, e1_gate, e1_up;
  logic signed [32*DATA_W-1:0] e0_silu, e1_silu, e0_mid, e1_mid;
  logic signed [64*DATA_W-1:0] e0_out, e1_out, moe_projected;
  logic signed [64*DATA_W-1:0] matvec_x;
  logic signed [512*DATA_W-1:0] logits_reg;
  logic [8:0] argmax_live;
  logic signed [DATA_W-1:0] argmax_value_live;
  integer i;
  logic signed [63:0] product0, product1, weighted_sum;

  matrix_qsz_buffer weights (
    .clk, .rst_n, .begin_load(buffer_begin),
    .expected_q_beats(matrix_q_beats), .expected_s_beats(matrix_s_beats),
    .expected_z_beats(matrix_z_beats), .dma_data_valid, .dma_data_ready,
    .section(dma_section), .dma_data, .all_loaded(matrix_loaded),
    .q_beats_loaded(q_loaded), .s_beats_loaded(s_loaded), .z_beats_loaded(z_loaded),
    .q_bus(q_buffer), .s_bus(s_buffer), .z_bus(z_buffer)
  );
  activation_bank activations (
    .clk, .rst_n, .write_valid(bank_write_valid), .write_slot(bank_write_slot),
    .write_bus(bank_write_bus), .read_slot0(bank_read_slot0), .read_bus0(bank_read0),
    .read_slot1(bank_read_slot1), .read_bus1(bank_read1)
  );
  logic signed [64*DATA_W-1:0] embedding_y;
  q4_embedding_lookup embed_lookup_y (
    .token_id(active_token_id), .q_bus(q_buffer), .scale_bus(s_buffer[512*16-1:0]),
    .zero_bus(z_buffer[512*8-1:0]), .embedding_bus(embedding_y)
  );
  q4_matvec_dispatch matvec (
    .kind(loaded_matrix_kind), .x_bus(matvec_x), .q_bus(q_buffer), .s_bus(s_buffer),
    .z_bus(z_buffer), .y_bus(matvec_y)
  );
  rmsnorm_q rms (.x_bus(bank_read0), .norm_offset_bus, .y_bus(rms_y));
  residual_mix2_scores #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) mix_scores (
    .score0(residual_score0), .score1(residual_score1), .weight0(residual_w0), .weight1(residual_w1)
  );
  residual_mix2 #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) mix (
    .in0_bus(bank_read0), .in1_bus(bank_read1), .weight0(residual_w0), .weight1(residual_w1), .out_bus(residual_y)
  );

  causal_conv4 q_conv_u (.clk, .rst_n, .in_valid(state == ST_KSTEP), .in_bus(q_pre), .taps_bus(kda_q_taps_bus), .pre_silu_bus(q_conv));
  causal_conv4 k_conv_u (.clk, .rst_n, .in_valid(state == ST_KSTEP), .in_bus(k_pre), .taps_bus(kda_k_taps_bus), .pre_silu_bus(k_conv));
  causal_conv4 v_conv_u (.clk, .rst_n, .in_valid(state == ST_KSTEP), .in_bus(v_pre), .taps_bus(kda_v_taps_bus), .pre_silu_bus(v_conv));
  generate
    genvar g;
    for (g = 0; g < 64; g = g + 1) begin : g_kda_silu_alpha
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) q_silu_u (.x_in(q_conv[g*DATA_W +: DATA_W]), .y_out(q_silu[g*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) k_silu_u (.x_in(k_conv[g*DATA_W +: DATA_W]), .y_out(k_silu[g*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) v_silu_u (.x_in(v_conv[g*DATA_W +: DATA_W]), .y_out(v_silu[g*DATA_W +: DATA_W]));
      logic [FRAC_W:0] alpha_probability;
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) alpha_u (.x_in(alpha_pre[g*DATA_W +: DATA_W]), .y_out(alpha_probability));
      always_comb alpha_gate[g*DATA_W +: DATA_W] = {{(DATA_W-FRAC_W-1){1'b0}}, alpha_probability};
    end
  endgenerate
  sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) beta_u (.x_in(beta_pre), .y_out(beta_gate));
  l2norm_q l2q0 (.x_bus(q_silu[0 +: 32*DATA_W]), .y_bus(q0_norm));
  l2norm_q l2q1 (.x_bus(q_silu[32*DATA_W +: 32*DATA_W]), .y_bus(q1_norm));
  l2norm_q l2k0 (.x_bus(k_silu[0 +: 32*DATA_W]), .y_bus(k0_norm));
  l2norm_q l2k1 (.x_bus(k_silu[32*DATA_W +: 32*DATA_W]), .y_bus(k1_norm));
  kda_delta_head kda0 (.clk, .rst_n, .step_valid(state == ST_KSTEP), .q_in(q0_norm), .k_in(k0_norm), .v_in(v_silu[0 +: 32*DATA_W]), .alpha_in(alpha_gate[0 +: 32*DATA_W]), .beta_in({{(DATA_W-FRAC_W-1){1'b0}}, beta_gate}), .out_bus(kda_out0));
  kda_delta_head kda1 (.clk, .rst_n, .step_valid(state == ST_KSTEP), .q_in(q1_norm), .k_in(k1_norm), .v_in(v_silu[32*DATA_W +: 32*DATA_W]), .alpha_in(alpha_gate[32*DATA_W +: 32*DATA_W]), .beta_in({{(DATA_W-FRAC_W-1){1'b0}}, beta_gate}), .out_bus(kda_out1));

  mla_kv_cache #(.MAX_SEQ(MAX_SEQ)) mla_cache (
    .clk, .rst_n, .write_valid(state == ST_MCACHE), .write_pos(active_token_index[5:0]),
    .write_key_bus(mla_k), .write_value_bus(mla_v), .read_pos(mla_cache_index),
    .read_key_bus(cache_key_bus), .read_value_bus(cache_value_bus)
  );
  mla_attention_head #(.MAX_SEQ(MAX_SEQ), .KD(48), .DV(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mla0 (
    .clk, .rst_n, .start(state == ST_MSTART), .valid_count({1'b0,active_token_index[5:0]} + 1'b1),
    .query_bus(mla_q[0 +: 48*DATA_W]), .cache_index(mla_cache_index),
    .cache_key_bus(cache_key_bus[0 +: 48*DATA_W]), .cache_value_bus(cache_value_bus[0 +: 32*DATA_W]),
    .busy(mla_head0_busy), .done(mla_head0_done), .context_bus(mla_ctx0)
  );
  mla_attention_head #(.MAX_SEQ(MAX_SEQ), .KD(48), .DV(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mla1 (
    .clk, .rst_n, .start(state == ST_MSTART), .valid_count({1'b0,active_token_index[5:0]} + 1'b1),
    .query_bus(mla_q[48*DATA_W +: 48*DATA_W]), .cache_index(),
    .cache_key_bus(cache_key_bus[48*DATA_W +: 48*DATA_W]), .cache_value_bus(cache_value_bus[32*DATA_W +: 32*DATA_W]),
    .busy(mla_head1_busy), .done(mla_head1_done), .context_bus(mla_ctx1)
  );

  generate
    for (g = 0; g < 8; g = g + 1) begin : g_moe_gate
      logic [FRAC_W:0] probability;
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) gate_u (.x_in(moe_gate_pre[g*DATA_W +: DATA_W]), .y_out(probability));
      always_comb moe_gate_scores[g*DATA_W +: DATA_W] = {{(DATA_W-FRAC_W-1){1'b0}}, probability};
    end
    for (g = 0; g < 32; g = g + 1) begin : g_moe_silu
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) e0_silu_u (.x_in(e0_gate[g*DATA_W +: DATA_W]), .y_out(e0_silu[g*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) e1_silu_u (.x_in(e1_gate[g*DATA_W +: DATA_W]), .y_out(e1_silu[g*DATA_W +: DATA_W]));
    end
  endgenerate
  moe_top2_router #(.SCORE_W(DATA_W), .FRAC_W(FRAC_W)) router (
    .score_bus(moe_gate_scores), .bias_bus(moe_bias_bus), .expert0(moe_expert0), .expert1(moe_expert1),
    .weight0(moe_weight0), .weight1(moe_weight1)
  );
  always_comb begin
    e0_mid = '0; e1_mid = '0;
    for (i = 0; i < 32; i = i + 1) begin
      product0 = $signed(e0_silu[i*DATA_W +: DATA_W]) * $signed(e0_up[i*DATA_W +: DATA_W]);
      product1 = $signed(e1_silu[i*DATA_W +: DATA_W]) * $signed(e1_up[i*DATA_W +: DATA_W]);
      e0_mid[i*DATA_W +: DATA_W] = product0 >>> FRAC_W;
      e1_mid[i*DATA_W +: DATA_W] = product1 >>> FRAC_W;
    end
    moe_projected = '0;
    for (i = 0; i < 64; i = i + 1) begin
      weighted_sum = $signed(e0_out[i*DATA_W +: DATA_W]) * moe_weight0 + $signed(e1_out[i*DATA_W +: DATA_W]) * moe_weight1;
      moe_projected[i*DATA_W +: DATA_W] = weighted_sum >>> FRAC_W;
    end
  end
  argmax_vector #(.ELEMS(512), .DATA_W(DATA_W)) argmax_u (.values_bus(matvec_y), .index_out(argmax_live), .value_out(argmax_value_live));

  always_comb begin
    matrix_req_valid = 1'b0; matrix_tensor_id = '0; matrix_expert_index = '0; matrix_kind = MK_64X64;
    case (state)
      ST_EMB_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_EMBED; matrix_kind=MK_EMBED; end
      ST_KQ_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_Q; end
      ST_KK_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_K; end
      ST_KV_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_V; end
      ST_KA_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_A; end
      ST_KB_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_B; matrix_kind=MK_64X1; end
      ST_KO_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_KDA_O; end
      ST_MQ_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MLA_Q; matrix_kind=MK_64X96; end
      ST_MK_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MLA_K; matrix_kind=MK_64X96; end
      ST_MV_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MLA_V; end
      ST_MO_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MLA_O; end
      ST_MG_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_GATE; matrix_kind=MK_64X8; end
      ST_E0G_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_G; matrix_expert_index=moe_expert0; matrix_kind=MK_64X32; end
      ST_E0U_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_U; matrix_expert_index=moe_expert0; matrix_kind=MK_64X32; end
      ST_E0D_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_D; matrix_expert_index=moe_expert0; matrix_kind=MK_32X64; end
      ST_E1G_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_G; matrix_expert_index=moe_expert1; matrix_kind=MK_64X32; end
      ST_E1U_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_U; matrix_expert_index=moe_expert1; matrix_kind=MK_64X32; end
      ST_E1D_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_MOE_D; matrix_expert_index=moe_expert1; matrix_kind=MK_32X64; end
      ST_HEAD_REQ: begin matrix_req_valid=1'b1; matrix_tensor_id=T_HEAD; matrix_kind=MK_64X512; end
      default: ;
    endcase
    case (matrix_kind)
      MK_64X64:  begin matrix_q_beats=16'd128; matrix_s_beats=16'd8;  matrix_z_beats=16'd2;  end
      MK_64X32:  begin matrix_q_beats=16'd64;  matrix_s_beats=16'd4;  matrix_z_beats=16'd1;  end
      MK_32X64:  begin matrix_q_beats=16'd64;  matrix_s_beats=16'd8;  matrix_z_beats=16'd2;  end
      MK_64X96:  begin matrix_q_beats=16'd192; matrix_s_beats=16'd12; matrix_z_beats=16'd3;  end
      MK_64X8:   begin matrix_q_beats=16'd16;  matrix_s_beats=16'd1;  matrix_z_beats=16'd1;  end
      MK_64X1:   begin matrix_q_beats=16'd2;   matrix_s_beats=16'd1;  matrix_z_beats=16'd1;  end
      // head.z has 512 packed nibbles = 256 B = 16 beats. Embedding is
      // token-major and has one uint8 zero per vocabulary row, hence 512 B.
      MK_64X512: begin matrix_q_beats=16'd1024;matrix_s_beats=16'd64; matrix_z_beats=16'd16; end
      MK_EMBED:  begin matrix_q_beats=16'd1024;matrix_s_beats=16'd64; matrix_z_beats=16'd32; end
      default:    begin matrix_q_beats='0; matrix_s_beats='0; matrix_z_beats='0; end
    endcase
    buffer_begin = matrix_req_valid && matrix_req_accept;
  end

  always_comb begin
    bank_read_slot0 = S_CUR; bank_read_slot1 = S_BRANCH;
    if (state == ST_KQ_REQ || state == ST_KQ_WAIT || state == ST_KK_REQ || state == ST_KK_WAIT ||
        state == ST_KV_REQ || state == ST_KV_WAIT || state == ST_KA_REQ || state == ST_KA_WAIT ||
        state == ST_KB_REQ || state == ST_KB_WAIT || state == ST_MQ_REQ || state == ST_MQ_WAIT ||
        state == ST_MK_REQ || state == ST_MK_WAIT || state == ST_MV_REQ || state == ST_MV_WAIT ||
        state == ST_MG_REQ || state == ST_MG_WAIT || state == ST_E0G_REQ || state == ST_E0G_WAIT ||
        state == ST_E0U_REQ || state == ST_E0U_WAIT || state == ST_E1G_REQ || state == ST_E1G_WAIT ||
        state == ST_E1U_REQ || state == ST_E1U_WAIT)
      bank_read_slot0 = S_NORM;
    if (state == ST_KO_REQ || state == ST_KO_WAIT) bank_read_slot0 = S_BRANCH;
    if (state == ST_MO_REQ || state == ST_MO_WAIT) bank_read_slot0 = S_BRANCH;
    if (state == ST_E0D_REQ || state == ST_E0D_WAIT || state == ST_E1D_REQ || state == ST_E1D_WAIT) bank_read_slot0 = S_BRANCH;
    if (state == ST_MIX_WRITE && active_op == OP_L0_RESMIX) bank_read_slot0 = S_X0;
  end
  always_comb begin
    matvec_x = bank_read0;
    if (state == ST_KO_REQ || state == ST_KO_WAIT) matvec_x = kda_step_result;
    else if (state == ST_MO_REQ || state == ST_MO_WAIT) matvec_x = mla_context;
    else if (state == ST_E0D_REQ || state == ST_E0D_WAIT) matvec_x = {{(32*DATA_W){1'b0}}, e0_mid};
    else if (state == ST_E1D_REQ || state == ST_E1D_WAIT) matvec_x = {{(32*DATA_W){1'b0}}, e1_mid};
  end
  always_comb begin
    bank_write_valid = 1'b0; bank_write_slot = S_CUR; bank_write_bus = '0;
    case (state)
      ST_EMB_W0:   begin bank_write_valid=1'b1; bank_write_slot=S_X0;     bank_write_bus=embedding_y; end
      ST_EMB_WCUR: begin bank_write_valid=1'b1; bank_write_slot=S_CUR;    bank_write_bus=embedding_y; end
      ST_NORM_WRITE: begin bank_write_valid=1'b1; bank_write_slot=S_NORM; bank_write_bus=rms_y; end
      ST_KWRITE:   begin bank_write_valid=1'b1; bank_write_slot=S_BRANCH; bank_write_bus=kda_projected; end
      ST_MIX_WRITE:begin bank_write_valid=1'b1; bank_write_slot=S_CUR;    bank_write_bus=residual_y; end
      ST_MWRITE:   begin bank_write_valid=1'b1; bank_write_slot=S_BRANCH; bank_write_bus=mla_projected; end
      ST_MOE_WRITE:begin bank_write_valid=1'b1; bank_write_slot=S_BRANCH; bank_write_bus=moe_projected; end
      default: ;
    endcase
  end
  always_comb begin
    mla_context = {mla_ctx1, mla_ctx0};
    logits_bus = logits_reg;
    op_done = (state == ST_COMPLETE);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE; active_op <= '0; active_token_index <= '0; active_token_id <= '0; loaded_matrix_kind <= MK_64X64;
      q_pre <= '0; k_pre <= '0; v_pre <= '0; alpha_pre <= '0; beta_pre <= '0;
      kda_step_result <= '0; kda_projected <= '0; mla_q <= '0; mla_k <= '0; mla_v <= '0; mla_projected <= '0;
      e0_gate <= '0; e0_up <= '0; e0_out <= '0; e1_gate <= '0; e1_up <= '0; e1_out <= '0;
      logits_reg <= '0; argmax_index <= '0; argmax_value <= '0;
    end else begin
      if (matrix_req_valid && matrix_req_accept)
        loaded_matrix_kind <= matrix_kind;
      case (state)
        ST_IDLE: if (op_valid) begin
          active_op <= op_code; active_token_index <= op_token_index; active_token_id <= token_id;
          case (op_code)
            OP_EMBED: state <= ST_EMB_REQ;
            OP_L0_NORM1, OP_L0_NORM2, OP_L1_NORM1, OP_L1_NORM2, OP_FINAL_NORM: state <= ST_NORM_WRITE;
            OP_L0_KDA: state <= ST_KQ_REQ;
            OP_L0_RESMIX, OP_L1_RESA, OP_L1_RESMIX, OP_OUT_RESMIX: state <= ST_MIX_WRITE;
            OP_L1_MLA: state <= ST_MQ_REQ;
            OP_L0_MOE, OP_L1_MOE: state <= ST_MG_REQ;
            OP_HEAD_ARGMAX: state <= ST_HEAD_REQ;
            default: state <= ST_COMPLETE;
          endcase
        end
        ST_EMB_REQ: if (matrix_req_accept) state <= ST_EMB_WAIT;
        ST_EMB_WAIT: if (matrix_loaded) state <= ST_EMB_W0;
        ST_EMB_W0: state <= ST_EMB_WCUR;
        ST_EMB_WCUR: state <= ST_COMPLETE;
        ST_NORM_WRITE, ST_KWRITE, ST_MIX_WRITE, ST_MWRITE, ST_MOE_WRITE: state <= ST_COMPLETE;
        ST_KQ_REQ: if (matrix_req_accept) state <= ST_KQ_WAIT;
        ST_KQ_WAIT: if (matrix_loaded) begin q_pre <= matvec_y[64*DATA_W-1:0]; state <= ST_KK_REQ; end
        ST_KK_REQ: if (matrix_req_accept) state <= ST_KK_WAIT;
        ST_KK_WAIT: if (matrix_loaded) begin k_pre <= matvec_y[64*DATA_W-1:0]; state <= ST_KV_REQ; end
        ST_KV_REQ: if (matrix_req_accept) state <= ST_KV_WAIT;
        ST_KV_WAIT: if (matrix_loaded) begin v_pre <= matvec_y[64*DATA_W-1:0]; state <= ST_KA_REQ; end
        ST_KA_REQ: if (matrix_req_accept) state <= ST_KA_WAIT;
        ST_KA_WAIT: if (matrix_loaded) begin alpha_pre <= matvec_y[64*DATA_W-1:0]; state <= ST_KB_REQ; end
        ST_KB_REQ: if (matrix_req_accept) state <= ST_KB_WAIT;
        ST_KB_WAIT: if (matrix_loaded) begin beta_pre <= matvec_y[DATA_W-1:0]; state <= ST_KSTEP; end
        // Capture q @ S_new in the clock edge that commits S_new.  In the
        // following cycle kda_delta_head's combinational view would instead
        // be the hypothetical next-token update.
        ST_KSTEP: begin kda_step_result <= {kda_out1, kda_out0}; state <= ST_KO_REQ; end
        ST_KO_REQ: if (matrix_req_accept) state <= ST_KO_WAIT;
        ST_KO_WAIT: if (matrix_loaded) begin kda_projected <= matvec_y[64*DATA_W-1:0]; state <= ST_KWRITE; end
        ST_MQ_REQ: if (matrix_req_accept) state <= ST_MQ_WAIT;
        ST_MQ_WAIT: if (matrix_loaded) begin mla_q <= matvec_y[96*DATA_W-1:0]; state <= ST_MK_REQ; end
        ST_MK_REQ: if (matrix_req_accept) state <= ST_MK_WAIT;
        ST_MK_WAIT: if (matrix_loaded) begin mla_k <= matvec_y[96*DATA_W-1:0]; state <= ST_MV_REQ; end
        ST_MV_REQ: if (matrix_req_accept) state <= ST_MV_WAIT;
        ST_MV_WAIT: if (matrix_loaded) begin mla_v <= matvec_y[64*DATA_W-1:0]; state <= ST_MCACHE; end
        ST_MCACHE: state <= ST_MSTART;
        ST_MSTART: state <= ST_MWAIT;
        ST_MWAIT: if (mla_head0_done && mla_head1_done) state <= ST_MO_REQ;
        ST_MO_REQ: if (matrix_req_accept) state <= ST_MO_WAIT;
        ST_MO_WAIT: if (matrix_loaded) begin mla_projected <= matvec_y[64*DATA_W-1:0]; state <= ST_MWRITE; end
        ST_MG_REQ: if (matrix_req_accept) state <= ST_MG_WAIT;
        ST_MG_WAIT: if (matrix_loaded) begin moe_gate_pre <= matvec_y[8*DATA_W-1:0]; state <= ST_E0G_REQ; end
        ST_E0G_REQ: if (matrix_req_accept) state <= ST_E0G_WAIT;
        ST_E0G_WAIT: if (matrix_loaded) begin e0_gate <= matvec_y[32*DATA_W-1:0]; state <= ST_E0U_REQ; end
        ST_E0U_REQ: if (matrix_req_accept) state <= ST_E0U_WAIT;
        ST_E0U_WAIT: if (matrix_loaded) begin e0_up <= matvec_y[32*DATA_W-1:0]; state <= ST_E0D_REQ; end
        ST_E0D_REQ: if (matrix_req_accept) state <= ST_E0D_WAIT;
        ST_E0D_WAIT: if (matrix_loaded) begin e0_out <= matvec_y[64*DATA_W-1:0]; state <= ST_E1G_REQ; end
        ST_E1G_REQ: if (matrix_req_accept) state <= ST_E1G_WAIT;
        ST_E1G_WAIT: if (matrix_loaded) begin e1_gate <= matvec_y[32*DATA_W-1:0]; state <= ST_E1U_REQ; end
        ST_E1U_REQ: if (matrix_req_accept) state <= ST_E1U_WAIT;
        ST_E1U_WAIT: if (matrix_loaded) begin e1_up <= matvec_y[32*DATA_W-1:0]; state <= ST_E1D_REQ; end
        ST_E1D_REQ: if (matrix_req_accept) state <= ST_E1D_WAIT;
        ST_E1D_WAIT: if (matrix_loaded) begin e1_out <= matvec_y[64*DATA_W-1:0]; state <= ST_MOE_WRITE; end
        ST_HEAD_REQ: if (matrix_req_accept) state <= ST_HEAD_WAIT;
        ST_HEAD_WAIT: if (matrix_loaded) begin logits_reg <= matvec_y; argmax_index <= argmax_live; argmax_value <= argmax_value_live; state <= ST_COMPLETE; end
        ST_COMPLETE: state <= ST_IDLE;
        default: state <= ST_IDLE;
      endcase
    end
  end
endmodule
