// Golden-model datapath scheduler. This is intentionally separate from the
// legacy private-ABI adapter. Named tensor loads use golden_name_hash_pkg and
// nano_golden_datapath_adapter exclusively.
module nano_golden_datapath_fsm #(
  parameter int DATA_W = 24, parameter int MAX_SEQ = 64
) (
  input logic clk, input logic rst_n, input logic begin_token,
  input logic [8:0] token_id, input logic [5:0] token_position,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic group_is_128,
  output logic busy, output logic token_done, output logic error,
  output logic signed [512*DATA_W-1:0] logits_bus,
  output logic [8:0] argmax_index,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
  import golden_name_hash_pkg::*;
  import golden_tensor_meta_pkg::*;
  typedef enum logic [7:0] {IDLE, EMB_REQ, EMB_WAIT, EMB_CAPTURE,
    L0_NORM1_REQ, L0_NORM1_WAIT, L0_NORM1_CAPTURE, KDA_BEGIN, MLA_BEGIN,
    KQ_REQ, KQ_WAIT, KQ_CAPTURE, KK_REQ, KK_WAIT, KK_CAPTURE, KV_REQ, KV_WAIT,
    KV_CAPTURE, KB_REQ, KB_WAIT, KB_CAPTURE, F1_REQ, F1_WAIT, F1_CAPTURE,
    F2_REQ, F2_WAIT, F2_CAPTURE, G1_REQ, G1_WAIT, G1_CAPTURE, G2_REQ, G2_WAIT,
    G2_CAPTURE, QCONV_REQ, QCONV_WAIT, QCONV_CAPTURE, KCONV_REQ, KCONV_WAIT,
    KCONV_CAPTURE, VCONV_REQ, VCONV_WAIT, VCONV_CAPTURE, KA_REQ, KA_WAIT,
    KA_CAPTURE, KDTB_REQ, KDTB_WAIT, KDTB_CAPTURE, KONORM_REQ, KONORM_WAIT,
    KONORM_CAPTURE, KDA_STEP, KO_REQ, KO_WAIT, KO_CAPTURE,
    RA_NORM_REQ, RA_NORM_WAIT, RA_NORM_CAPTURE, RA_PROJ_REQ, RA_PROJ_WAIT,
    RA_PROJ_CAPTURE, RA_MIX, L0_NORM2_REQ, L0_NORM2_WAIT, L0_NORM2_CAPTURE,
    L0_BIAS_REQ, L0_BIAS_WAIT, L0_BIAS_CAPTURE, L0_ROUTER_REQ, L0_ROUTER_WAIT,
    L0_ROUTER_CAPTURE, L0_ROUTE_CAPTURE,
    SG_REQ, SG_WAIT, SG_CAPTURE, SU_REQ, SU_WAIT, SU_CAPTURE, SD_REQ, SD_WAIT, SD_CAPTURE,
    E0G_REQ, E0G_WAIT, E0G_CAPTURE, E0U_REQ, E0U_WAIT, E0U_CAPTURE, E0D_REQ, E0D_WAIT, E0D_CAPTURE,
    E1G_REQ, E1G_WAIT, E1G_CAPTURE, E1U_REQ, E1U_WAIT, E1U_CAPTURE, E1D_REQ, E1D_WAIT, E1D_CAPTURE,
    L0_MOE_MIX, RM_NORM_REQ, RM_NORM_WAIT, RM_NORM_CAPTURE, RM_PROJ_REQ,
    RM_PROJ_WAIT, RM_PROJ_CAPTURE, RM_MIX,
    L1_RA_NORM_REQ, L1_RA_NORM_WAIT, L1_RA_NORM_CAPTURE, L1_RA_PROJ_REQ,
    L1_RA_PROJ_WAIT, L1_RA_PROJ_CAPTURE, L1_RA_MIX, L1_NORM1_REQ,
    L1_NORM1_WAIT, L1_NORM1_CAPTURE,
    L1_WC_REQ, L1_WC_WAIT, L1_WC_CAPTURE, L1_KNORM_REQ, L1_KNORM_WAIT,
    L1_KNORM_CAPTURE, L1_WQC_REQ, L1_WQC_WAIT, L1_WQC_CAPTURE, L1_WQR_REQ,
    L1_WQR_WAIT, L1_WQR_CAPTURE, L1_WKR_REQ, L1_WKR_WAIT, L1_WKR_CAPTURE,
    L1_WKC_REQ, L1_WKC_WAIT, L1_WKC_CAPTURE, L1_WV_REQ, L1_WV_WAIT,
    L1_WV_CAPTURE, L1_ATTN_START, L1_ATTN_WAIT, L1_O_REQ, L1_O_WAIT, L1_O_CAPTURE,
    L1_RM_NORM_REQ, L1_RM_NORM_WAIT, L1_RM_NORM_CAPTURE, L1_RM_PROJ_REQ,
    L1_RM_PROJ_WAIT, L1_RM_PROJ_CAPTURE, L1_RM_MIX, L1_NORM2_REQ,
    L1_NORM2_WAIT, L1_NORM2_CAPTURE,
    L1_BIAS_REQ, L1_BIAS_WAIT, L1_BIAS_CAPTURE, L1_ROUTER_REQ, L1_ROUTER_WAIT,
    L1_ROUTER_CAPTURE, L1_ROUTE_CAPTURE,
    L1_SG_REQ, L1_SG_WAIT, L1_SG_CAPTURE, L1_SU_REQ, L1_SU_WAIT, L1_SU_CAPTURE,
    L1_SD_REQ, L1_SD_WAIT, L1_SD_CAPTURE, L1_E0G_REQ, L1_E0G_WAIT, L1_E0G_CAPTURE,
    L1_E0U_REQ, L1_E0U_WAIT, L1_E0U_CAPTURE, L1_E0D_REQ, L1_E0D_WAIT, L1_E0D_CAPTURE,
    L1_E1G_REQ, L1_E1G_WAIT, L1_E1G_CAPTURE, L1_E1U_REQ, L1_E1U_WAIT, L1_E1U_CAPTURE,
    L1_E1D_REQ, L1_E1D_WAIT, L1_E1D_CAPTURE, L1_MOE_MIX,
    OUT_RM_NORM_REQ, OUT_RM_NORM_WAIT, OUT_RM_NORM_CAPTURE, OUT_RM_PROJ_REQ,
    OUT_RM_PROJ_WAIT, OUT_RM_PROJ_CAPTURE, OUT_RM_MIX, FINAL_NORM_REQ,
    FINAL_NORM_WAIT, FINAL_NORM_CAPTURE, HEAD_REQ, HEAD_WAIT, HEAD_CAPTURE,
    MOE_BEGIN, HEAD_BEGIN, COMPLETE, FAILED} state_t;
  state_t state;
  logic load_valid, load_accept, load_is_p16, load_busy, load_done, load_error;
  logic [31:0] qh,qb,sh,sb,zh,zb,ph,pb;
  logic [1024*128-1:0] q_bus; logic [128*128-1:0] s_bus,z_bus; logic [64*128-1:0] p_bus;
  logic signed [64*DATA_W-1:0] embedding_bus, x_bus, h_norm, h_norm_reg, q_pre, k_pre, v_pre;
  logic [31:0] qpre_qsig, kpre_qsig, vpre_qsig;
  logic signed [2*DATA_W-1:0] beta_pre, beta_reg;
  logic signed [32*DATA_W-1:0] f_mid, g_mid;
  logic signed [64*DATA_W-1:0] f_calc, z_calc, f_reg, z_reg;
  logic signed [64*4*16-1:0] qconv_reg, kconv_reg, vconv_reg;
  logic [2*16-1:0] kda_a_reg;
  logic signed [64*16-1:0] kda_dtb_reg;
  logic signed [32*16-1:0] kda_onorm_reg;
  logic kda_step_valid;
  logic signed [64*DATA_W-1:0] kda_core, kda_core_reg, kda_attention;
  // Token-local KDA snapshots for fixed-point correlation.  They are sampled
  // on the recurrence edge, before causal-conv history updates are visible.
  logic signed [64*DATA_W-1:0] kda_qconv_dbg, kda_kconv_dbg, kda_vconv_dbg;
  logic signed [64*DATA_W-1:0] kda_qact_dbg, kda_kact_dbg, kda_vact_dbg;
  logic signed [64*DATA_W-1:0] kda_qnorm_dbg, kda_knorm_dbg, kda_alpha_dbg;
  logic signed [64*DATA_W-1:0] kda_head_raw_dbg, kda_head_norm_dbg;
  logic signed [2*DATA_W-1:0] kda_beta_dbg;
  logic signed [64*DATA_W-1:0] l0_block, l0_attn_prefix, l0_after_attn, l0_moe_norm, l0_moe_prefix, l0_mlp_mix, l0_out;
  logic signed [64*16-1:0] l0_res_attn_norm, l0_res_attn_proj, l0_norm2_offset, l0_res_mlp_norm, l0_res_mlp_proj;
  logic signed [64*DATA_W-1:0] l1_attn_mixed, l1_norm1;
  logic signed [64*DATA_W-1:0] l1_prefix, l1_mlp_mix, l1_moe_norm;
  logic signed [64*16-1:0] l1_res_attn_norm, l1_res_attn_proj, l1_norm1_offset;
  logic signed [64*16-1:0] l1_res_mlp_norm, l1_res_mlp_proj, l1_norm2_offset;
  logic signed [128*DATA_W-1:0] l1_wc_pre, l1_latent;
  logic signed [64*DATA_W-1:0] l1_qc_pre, l1_kc, l1_value, l1_mla_attention;
  logic signed [32*DATA_W-1:0] l1_qr_pre;
  logic signed [16*DATA_W-1:0] l1_kr_pre;
  logic signed [128*16-1:0] l1_knorm_offset;
  logic signed [2*48*DATA_W-1:0] l1_query;
  logic signed [2*16*DATA_W-1:0] l1_shared_kr;
  logic l1_attn_start, l1_attn_busy, l1_attn_done;
  logic signed [64*DATA_W-1:0] l1_context;
  logic signed [8*DATA_W-1:0] l1_router_pre, l1_router_pre_reg, l1_bias;
  logic signed [8*16-1:0] l1_bias_q11;
  logic [2:0] l1_selected0, l1_selected1, l1_route0, l1_route1;
  logic signed [DATA_W-1:0] l1_route_w0, l1_route_w1;
  logic signed [64*DATA_W-1:0] l1_zero_expert, l1_router_mix_unused, l1_moe_out, l1_out, l1_out_next;
  logic signed [64*DATA_W-1:0] out_res_mix, final_hidden;
  logic signed [64*16-1:0] out_res_norm, out_res_proj, final_norm_offset;
  logic signed [512*DATA_W-1:0] head_g128, head_calc, logits_reg;
  logic [8:0] head_argmax_calc, argmax_reg;
  logic head_active;
  logic [1024*128-1:0] head_q_input;
  logic [128*128-1:0] head_s_input, head_z_input;
  logic [64/2*32*8-1:0] l1_sgq, l1_suq, l1_e0gq, l1_e0uq, l1_e1gq, l1_e1uq;
  logic signed [32*16-1:0] l1_sgs, l1_sus, l1_e0gs, l1_e0us, l1_e1gs, l1_e1us;
  logic [32*8-1:0] l1_sgz, l1_suz, l1_e0gz, l1_e0uz, l1_e1gz, l1_e1uz;
  logic [32/2*64*8-1:0] l1_sdq, l1_e0dq, l1_e1dq;
  logic signed [64*16-1:0] l1_sds, l1_e0ds, l1_e1ds;
  logic [64*8-1:0] l1_sdz, l1_e0dz, l1_e1dz;
  logic signed [64*DATA_W-1:0] l1_shared_expert, l1_selected_expert0, l1_selected_expert1;
  logic signed [128*DATA_W-1:0] l1_wc_calc;
  logic signed [64*DATA_W-1:0] l1_qc_calc, l1_kc_calc, l1_value_calc, l1_o_calc;
  logic signed [32*DATA_W-1:0] l1_qr_calc;
  logic signed [16*DATA_W-1:0] l1_kr_calc;
  logic signed [8*DATA_W-1:0] l0_router_pre, l0_bias;
  logic signed [8*16-1:0] l0_bias_q11;
  logic [2:0] l0_selected0, l0_selected1, l0_route0, l0_route1;
  logic signed [DATA_W-1:0] l0_route_w0, l0_route_w1;
  logic [64/2*32*8-1:0] sgq, suq, e0gq, e0uq, e1gq, e1uq;
  logic signed [32*16-1:0] sgs, sus, e0gs, e0us, e1gs, e1us;
  logic [32*8-1:0] sgz, suz, e0gz, e0uz, e1gz, e1uz;
  logic [32/2*64*8-1:0] sdq, e0dq, e1dq;
  logic signed [64*16-1:0] sds, e0ds, e1ds;
  logic [64*8-1:0] sdz, e0dz, e1dz;
  logic signed [64*DATA_W-1:0] shared_expert, selected_expert0, selected_expert1, l0_moe_out;
  qsz_meta_t dynamic_meta;
  logic [7:0] dynamic_matrix_id;
  logic dynamic_qsz_load;
  logic signed [512*DATA_W-1:0] matvec_y;
  logic signed [64*DATA_W-1:0] matvec_x;
  logic signed [64*DATA_W-1:0] matvec_y_64;
  logic signed [32*DATA_W-1:0] matvec_y_32;
  logic signed [8*DATA_W-1:0] matvec_y_8;
  logic [3:0] matvec_kind;

  nano_golden_datapath_adapter loads (
    .clk, .rst_n, .descriptor_base, .descriptor_count, .load_valid, .load_accept, .load_is_p16,
    .q_hash(qh), .q_bytes(qb), .s_hash(sh), .s_bytes(sb), .z_hash(zh), .z_bytes(zb), .p_hash(ph), .p_bytes(pb),
    .busy(load_busy), .loaded(load_done), .error(load_error), .q_bus, .s_bus, .z_bus, .p_bus,
    .desc_mem_req_valid, .desc_mem_req_ready, .desc_mem_req_addr, .desc_mem_rsp_valid, .desc_mem_rsp_ready, .desc_mem_rsp_rdata,
    .dma_mem_req_valid, .dma_mem_req_ready, .dma_mem_req_addr, .dma_mem_rsp_valid, .dma_mem_rsp_ready, .dma_mem_rsp_rdata);
  q4_embedding_lookup emb (.token_id, .q_bus, .scale_bus(s_bus[512*16-1:0]), .zero_bus(z_bus[512*8-1:0]), .embedding_bus);
  rmsnorm_q #(.DIM(64), .DATA_W(DATA_W)) norm1 (.x_bus(x_bus), .norm_offset_bus(p_bus[64*16-1:0]), .y_bus(h_norm));
  // The recurrence advances its state and causal histories on KDA_STEP.
  // Capture its combinational result in that same edge; re-evaluating
  // kda_core later would use the advanced state/history and turn one token
  // into a second, incorrect recurrence update.
  assign matvec_x = (state == KO_REQ || state == KO_WAIT || state == KO_CAPTURE) ? kda_core_reg :
                    (state == L0_ROUTER_REQ || state == L0_ROUTER_WAIT || state == L0_ROUTER_CAPTURE) ? l0_moe_norm : h_norm_reg;
  // The golden FSM uses only three of the generic dispatch shapes (64x64 for
  // KDA q/k/v/o, 64x32 for f1/g1, 64x8 for the L0 router).  The generic
  // q4_matvec_dispatch evaluates all seven shapes -- including a 64x512 head
  // -- every cycle, which dominates simulation time.  Dedicated instances
  // keep the per-cycle combinational cost at the shapes that are actually
  // selected; matvec_y's low lanes are numerically identical to the dispatch
  // output for the three used kinds.
  q4_dequant_matvec #(.IN(64), .OUT(64), .GROUP(64), .DATA_W(DATA_W)) mm_64x64
    (.x_bus(matvec_x), .q_bus(q_bus[64/2*64*8-1:0]), .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(matvec_y_64));
  q4_dequant_matvec #(.IN(64), .OUT(32), .GROUP(64), .DATA_W(DATA_W)) mm_64x32
    (.x_bus(matvec_x), .q_bus(q_bus[64/2*32*8-1:0]), .scale_bus(s_bus[32*16-1:0]), .zero_bus(z_bus[32*8-1:0]), .y_bus(matvec_y_32));
  q4_dequant_matvec #(.IN(64), .OUT(8), .GROUP(64), .DATA_W(DATA_W)) mm_64x8
    (.x_bus(matvec_x), .q_bus(q_bus[64/2*8*8-1:0]), .scale_bus(s_bus[8*16-1:0]), .zero_bus(z_bus[8*8-1:0]), .y_bus(matvec_y_8));
  always_comb begin
    matvec_y = '0;
    case (matvec_kind)
      4'd0:   matvec_y[64*DATA_W-1:0] = matvec_y_64;
      4'd1:   matvec_y[32*DATA_W-1:0] = matvec_y_32;
      4'd4:   matvec_y[8*DATA_W-1:0] = matvec_y_8;
      default: ;
    endcase
  end
  q4_dequant_matvec #(.IN(64), .OUT(2), .GROUP(64), .DATA_W(DATA_W)) kda_b_mm
    (.x_bus(h_norm_reg), .q_bus(q_bus[64/2*2*8-1:0]), .scale_bus(s_bus[2*16-1:0]), .zero_bus(z_bus[2*8-1:0]), .y_bus(beta_pre));
  q4_dequant_matvec #(.IN(32), .OUT(64), .GROUP(32), .DATA_W(DATA_W)) kda_f2_mm
    (.x_bus(f_mid), .q_bus(q_bus[32/2*64*8-1:0]), .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(f_calc));
  q4_dequant_matvec #(.IN(32), .OUT(64), .GROUP(32), .DATA_W(DATA_W)) kda_g2_mm
    (.x_bus(g_mid), .q_bus(q_bus[32/2*64*8-1:0]), .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(z_calc));
  kda_recurrence_contract #(.DATA_W(DATA_W)) kda_recurrence (
    .clk, .rst_n, .step_valid(kda_step_valid), .q_pre_bus(q_pre), .k_pre_bus(k_pre), .v_pre_bus(v_pre),
    .qconv_bus(qconv_reg), .kconv_bus(kconv_reg), .vconv_bus(vconv_reg), .b_bus(beta_reg),
    .f_bus(f_reg), .z_bus(z_reg), .A_bus(kda_a_reg), .dtb_bus(kda_dtb_reg),
    .onorm_offset_bus(kda_onorm_reg), .core_bus(kda_core));
  residual_mix2_contract #(.DATA_W(DATA_W)) l0_attn_residual (
    .block_bus(l0_block), .prefix_bus(l0_attn_prefix), .norm_offset_bus(l0_res_attn_norm),
    .proj_bus(l0_res_attn_proj), .block_score(), .prefix_score(), .out_bus(l0_after_attn));
  residual_mix2_contract #(.DATA_W(DATA_W)) l0_mlp_residual (
    .block_bus(l0_block), .prefix_bus(kda_attention), .norm_offset_bus(l0_res_mlp_norm),
    .proj_bus(l0_res_mlp_proj), .block_score(), .prefix_score(), .out_bus(l0_mlp_mix));
  residual_mix2_contract #(.DATA_W(DATA_W)) l1_attn_residual (
    .block_bus(l0_block), .prefix_bus(l0_out), .norm_offset_bus(l1_res_attn_norm),
    .proj_bus(l1_res_attn_proj), .block_score(), .prefix_score(), .out_bus(l1_attn_mixed));
  residual_mix2_contract #(.DATA_W(DATA_W)) l1_mlp_residual (
    .block_bus(l0_block), .prefix_bus(l1_prefix), .norm_offset_bus(l1_res_mlp_norm),
    .proj_bus(l1_res_mlp_proj), .block_score(), .prefix_score(), .out_bus(l1_mlp_mix));
  rmsnorm_q #(.DIM(64), .DATA_W(DATA_W)) l1_norm1_unit
    (.x_bus(l1_attn_mixed), .norm_offset_bus(l1_norm1_offset), .y_bus(l1_norm1));
  rmsnorm_q #(.DIM(64), .DATA_W(DATA_W)) l1_norm2_unit
    (.x_bus(l1_mlp_mix), .norm_offset_bus(l1_norm2_offset), .y_bus(l1_moe_norm));
  q4_dequant_matvec #(.IN(64), .OUT(128), .GROUP(64), .DATA_W(DATA_W)) l1_wc_mm
    (.x_bus(l1_norm1), .q_bus(q_bus[64/2*128*8-1:0]), .scale_bus(s_bus[128*16-1:0]), .zero_bus(z_bus[128*8-1:0]), .y_bus(l1_wc_calc));
  q4_dequant_matvec #(.IN(64), .OUT(64), .GROUP(64), .DATA_W(DATA_W)) l1_wqc_mm
    (.x_bus(l1_norm1), .q_bus(q_bus[64/2*64*8-1:0]), .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(l1_qc_calc));
  q4_dequant_matvec #(.IN(64), .OUT(32), .GROUP(64), .DATA_W(DATA_W)) l1_wqr_mm
    (.x_bus(l1_norm1), .q_bus(q_bus[64/2*32*8-1:0]), .scale_bus(s_bus[32*16-1:0]), .zero_bus(z_bus[32*8-1:0]), .y_bus(l1_qr_calc));
  q4_dequant_matvec #(.IN(64), .OUT(16), .GROUP(64), .DATA_W(DATA_W)) l1_wkr_mm
    (.x_bus(l1_norm1), .q_bus(q_bus[64/2*16*8-1:0]), .scale_bus(s_bus[16*16-1:0]), .zero_bus(z_bus[16*8-1:0]), .y_bus(l1_kr_calc));
  mla_latent_stage #(.DATA_W(DATA_W)) l1_latent_stage
    (.wc_pre_bus(l1_wc_pre), .knorm_offset_bus(l1_knorm_offset), .qc_pre_bus(l1_qc_pre), .qr_pre_bus(l1_qr_pre),
    .kr_pre_bus(l1_kr_pre), .latent_bus(l1_latent), .query_bus(l1_query), .shared_kr_bus(l1_shared_kr));
  q4_matvec_128x64 #(.DATA_W(DATA_W)) l1_wkc_mm
    (.group_is_128, .x_bus(l1_latent), .q_bus(q_bus[128/2*64*8-1:0]), .s_bus(s_bus[2*64*16-1:0]), .z_bus(z_bus[2*64*8-1:0]), .y_bus(l1_kc_calc));
  q4_matvec_128x64 #(.DATA_W(DATA_W)) l1_wv_mm
    (.group_is_128, .x_bus(l1_latent), .q_bus(q_bus[128/2*64*8-1:0]), .s_bus(s_bus[2*64*16-1:0]), .z_bus(z_bus[2*64*8-1:0]), .y_bus(l1_value_calc));
  mla_cache_attention_contract #(.MAX_SEQ(MAX_SEQ), .DATA_W(DATA_W)) l1_cache_attention
    (.clk, .rst_n, .start(l1_attn_start), .position(token_position), .query_bus(l1_query), .kc_bus(l1_kc),
    .shared_kr_bus(l1_shared_kr), .value_bus(l1_value), .busy(l1_attn_busy), .done(l1_attn_done), .context_bus(l1_context));
  q4_dequant_matvec #(.IN(64), .OUT(64), .GROUP(64), .DATA_W(DATA_W)) l1_o_mm
    (.x_bus(l1_context), .q_bus(q_bus[64/2*64*8-1:0]), .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(l1_o_calc));
  q4_dequant_matvec #(.IN(64), .OUT(8), .GROUP(64), .DATA_W(DATA_W)) l1_router_mm
    (.x_bus(l1_moe_norm), .q_bus(q_bus[64/2*8*8-1:0]), .scale_bus(s_bus[8*16-1:0]), .zero_bus(z_bus[8*8-1:0]), .y_bus(l1_router_pre));
  // Expert result ports are temporarily zero only until their named tensor
  // streams are installed; selection is already fully data-dependent here.
  moe_shared_top2_contract #(.DATA_W(DATA_W)) l1_route_contract
    (.router_pre_bus(l1_router_pre_reg), .bias_bus(l1_bias), .shared_bus(l1_shared_expert), .expert0_bus(l1_selected_expert0),
    .expert1_bus(l1_selected_expert1), .expert0(l1_route0), .expert1(l1_route1), .weight0(l1_route_w0), .weight1(l1_route_w1), .out_bus(l1_moe_out));
  swiglu_expert #(.DATA_W(DATA_W)) l1_shared (
    .h_bus(l1_moe_norm), .g_q_bus(l1_sgq), .g_s_bus(l1_sgs), .g_z_bus(l1_sgz), .u_q_bus(l1_suq), .u_s_bus(l1_sus), .u_z_bus(l1_suz),
    .d_q_bus(l1_sdq), .d_s_bus(l1_sds), .d_z_bus(l1_sdz), .out_bus(l1_shared_expert));
  swiglu_expert #(.DATA_W(DATA_W)) l1_expert0 (
    .h_bus(l1_moe_norm), .g_q_bus(l1_e0gq), .g_s_bus(l1_e0gs), .g_z_bus(l1_e0gz), .u_q_bus(l1_e0uq), .u_s_bus(l1_e0us), .u_z_bus(l1_e0uz),
    .d_q_bus(l1_e0dq), .d_s_bus(l1_e0ds), .d_z_bus(l1_e0dz), .out_bus(l1_selected_expert0));
  swiglu_expert #(.DATA_W(DATA_W)) l1_expert1 (
    .h_bus(l1_moe_norm), .g_q_bus(l1_e1gq), .g_s_bus(l1_e1gs), .g_z_bus(l1_e1gz), .u_q_bus(l1_e1uq), .u_s_bus(l1_e1us), .u_z_bus(l1_e1uz),
    .d_q_bus(l1_e1dq), .d_s_bus(l1_e1ds), .d_z_bus(l1_e1dz), .out_bus(l1_selected_expert1));
  residual_mix2_contract #(.DATA_W(DATA_W)) output_residual (
    .block_bus(l0_block), .prefix_bus(l1_out), .norm_offset_bus(out_res_norm), .proj_bus(out_res_proj),
    .block_score(), .prefix_score(), .out_bus(out_res_mix));
  rmsnorm_q #(.DIM(64), .DATA_W(DATA_W)) final_norm_unit
    (.x_bus(out_res_mix), .norm_offset_bus(final_norm_offset), .y_bus(final_hidden));
  // IN=64 fits in a single group for both group-size 64 and 128 (the head's
  // fan-in is 64), so the GROUP=64 head matvec is numerically identical to
  // the GROUP=128 one.  Keeping only the latter halves the head matvec cost.
  q4_dequant_matvec #(.IN(64), .OUT(512), .GROUP(128), .DATA_W(DATA_W)) head_mm_g128
    (.x_bus(final_hidden), .q_bus(head_q_input), .scale_bus(head_s_input[512*16-1:0]), .zero_bus(head_z_input[512*8-1:0]), .y_bus(head_g128));
  assign head_q_input = head_active ? q_bus : '0;
  assign head_s_input = head_active ? s_bus : '0;
  assign head_z_input = head_active ? z_bus : '0;
  rmsnorm_q #(.DIM(64), .DATA_W(DATA_W)) l0_norm2
    (.x_bus(l0_mlp_mix), .norm_offset_bus(l0_norm2_offset), .y_bus(l0_moe_norm));
  swiglu_expert #(.DATA_W(DATA_W)) l0_shared (
    .h_bus(l0_moe_norm), .g_q_bus(sgq), .g_s_bus(sgs), .g_z_bus(sgz), .u_q_bus(suq), .u_s_bus(sus), .u_z_bus(suz),
    .d_q_bus(sdq), .d_s_bus(sds), .d_z_bus(sdz), .out_bus(shared_expert));
  swiglu_expert #(.DATA_W(DATA_W)) l0_expert0 (
    .h_bus(l0_moe_norm), .g_q_bus(e0gq), .g_s_bus(e0gs), .g_z_bus(e0gz), .u_q_bus(e0uq), .u_s_bus(e0us), .u_z_bus(e0uz),
    .d_q_bus(e0dq), .d_s_bus(e0ds), .d_z_bus(e0dz), .out_bus(selected_expert0));
  swiglu_expert #(.DATA_W(DATA_W)) l0_expert1 (
    .h_bus(l0_moe_norm), .g_q_bus(e1gq), .g_s_bus(e1gs), .g_z_bus(e1gz), .u_q_bus(e1uq), .u_s_bus(e1us), .u_z_bus(e1uz),
    .d_q_bus(e1dq), .d_s_bus(e1ds), .d_z_bus(e1dz), .out_bus(selected_expert1));
  moe_shared_top2_contract #(.DATA_W(DATA_W)) l0_moe (
    .router_pre_bus(l0_router_pre), .bias_bus(l0_bias), .shared_bus(shared_expert), .expert0_bus(selected_expert0),
    .expert1_bus(selected_expert1), .expert0(l0_route0), .expert1(l0_route1), .weight0(l0_route_w0), .weight1(l0_route_w1), .out_bus(l0_moe_out));

  always_comb begin
    load_valid = (state == EMB_REQ) || (state == L0_NORM1_REQ) || (state == KQ_REQ) || (state == KK_REQ) || (state == KV_REQ) || (state == KB_REQ) ||
                 (state == F1_REQ) || (state == F2_REQ) || (state == G1_REQ) || (state == G2_REQ) ||
                 (state == QCONV_REQ) || (state == KCONV_REQ) || (state == VCONV_REQ) || (state == KA_REQ) ||
                 (state == KDTB_REQ) || (state == KONORM_REQ) || (state == KO_REQ) ||
                 (state == RA_NORM_REQ) || (state == RA_PROJ_REQ) || (state == L0_NORM2_REQ) ||
                 (state == L0_BIAS_REQ) || (state == L0_ROUTER_REQ) ||
                 (state == RM_NORM_REQ) || (state == RM_PROJ_REQ) ||
                 (state == L1_RA_NORM_REQ) || (state == L1_RA_PROJ_REQ) || (state == L1_NORM1_REQ) ||
                 (state == L1_KNORM_REQ) || (state == L1_WC_REQ) || (state == L1_WQC_REQ) ||
                 (state == L1_WQR_REQ) || (state == L1_WKR_REQ) || (state == L1_WKC_REQ) ||
                 (state == L1_WV_REQ) || (state == L1_O_REQ) || (state == L1_RM_NORM_REQ) ||
                 (state == L1_RM_PROJ_REQ) || (state == L1_NORM2_REQ) || (state == L1_BIAS_REQ) ||
                 (state == L1_ROUTER_REQ) || (state == OUT_RM_NORM_REQ) || (state == OUT_RM_PROJ_REQ) ||
                 (state == FINAL_NORM_REQ) || (state == HEAD_REQ);
    load_is_p16 = (state == L0_NORM1_REQ) || (state == QCONV_REQ) || (state == KCONV_REQ) ||
                  (state == VCONV_REQ) || (state == KA_REQ) || (state == KDTB_REQ) || (state == KONORM_REQ) ||
                  (state == RA_NORM_REQ) || (state == RA_PROJ_REQ) || (state == L0_NORM2_REQ) || (state == L0_BIAS_REQ) ||
                  (state == RM_NORM_REQ) || (state == RM_PROJ_REQ) ||
                  (state == L1_RA_NORM_REQ) || (state == L1_RA_PROJ_REQ) || (state == L1_NORM1_REQ) || (state == L1_KNORM_REQ) ||
                  (state == L1_RM_NORM_REQ) || (state == L1_RM_PROJ_REQ) || (state == L1_NORM2_REQ) || (state == L1_BIAS_REQ) ||
                  (state == OUT_RM_NORM_REQ) || (state == OUT_RM_PROJ_REQ) || (state == FINAL_NORM_REQ);
    qh=H_EMB_Q; qb=32'd16384; sh=H_EMB_S; sb=32'd1024; zh=H_EMB_Z; zb=32'd512;
    ph=H_L0_NORM1_W; pb=32'd128;
    if (state == KQ_REQ) begin qh=H_L0_KDA_Q_Q; qb=32'd2048; sh=H_L0_KDA_Q_S; sb=32'd128; zh=H_L0_KDA_Q_Z; zb=32'd64; end
    if (state == KK_REQ) begin qh=H_L0_KDA_K_Q; qb=32'd2048; sh=H_L0_KDA_K_S; sb=32'd128; zh=H_L0_KDA_K_Z; zb=32'd64; end
    if (state == KV_REQ) begin qh=H_L0_KDA_V_Q; qb=32'd2048; sh=H_L0_KDA_V_S; sb=32'd128; zh=H_L0_KDA_V_Z; zb=32'd64; end
    if (state == KB_REQ) begin qh=H_L0_KDA_B_Q; qb=32'd64; sh=H_L0_KDA_B_S; sb=32'd4; zh=H_L0_KDA_B_Z; zb=32'd2; end
    if (state == F1_REQ) begin qh=H_L0_KDA_F1_Q; qb=32'd1024; sh=H_L0_KDA_F1_S; sb=32'd64; zh=H_L0_KDA_F1_Z; zb=32'd32; end
    if (state == F2_REQ) begin qh=H_L0_KDA_F2_Q; qb=32'd1024; sh=H_L0_KDA_F2_S; sb=32'd128; zh=H_L0_KDA_F2_Z; zb=32'd64; end
    if (state == G1_REQ) begin qh=H_L0_KDA_G1_Q; qb=32'd1024; sh=H_L0_KDA_G1_S; sb=32'd64; zh=H_L0_KDA_G1_Z; zb=32'd32; end
    if (state == G2_REQ) begin qh=H_L0_KDA_G2_Q; qb=32'd1024; sh=H_L0_KDA_G2_S; sb=32'd128; zh=H_L0_KDA_G2_Z; zb=32'd64; end
    // p_bus is in native descriptor byte order: int16 element i occupies
    // p_bus[i*16 +: 16], matching the contract modules' packed inputs.
    if (state == QCONV_REQ) begin ph=H_L0_KDA_QCONV; pb=32'd512; end
    if (state == KCONV_REQ) begin ph=H_L0_KDA_KCONV; pb=32'd512; end
    if (state == VCONV_REQ) begin ph=H_L0_KDA_VCONV; pb=32'd512; end
    if (state == KA_REQ) begin ph=H_L0_KDA_A; pb=32'd4; end
    if (state == KDTB_REQ) begin ph=H_L0_KDA_DTB; pb=32'd128; end
    if (state == KONORM_REQ) begin ph=H_L0_KDA_ONORM_W; pb=32'd64; end
    if (state == KO_REQ) begin qh=H_L0_KDA_O_Q; qb=32'd2048; sh=H_L0_KDA_O_S; sb=32'd128; zh=H_L0_KDA_O_Z; zb=32'd64; end
    if (state == RA_NORM_REQ) begin ph=H_L0_RES_ATTN_NORM_W; pb=32'd128; end
    if (state == RA_PROJ_REQ) begin ph=H_L0_RES_ATTN_PROJ; pb=32'd128; end
    if (state == L0_NORM2_REQ) begin ph=H_L0_NORM2_W; pb=32'd128; end
    if (state == L0_BIAS_REQ) begin ph=H_L0_MOE_BIAS; pb=32'd16; end
    if (state == RM_NORM_REQ) begin ph=H_L0_RES_MLP_NORM_W; pb=32'd128; end
    if (state == RM_PROJ_REQ) begin ph=H_L0_RES_MLP_PROJ; pb=32'd128; end
    if (state == L1_RA_NORM_REQ) begin ph=H_L1_RES_ATTN_NORM_W; pb=32'd128; end
    if (state == L1_RA_PROJ_REQ) begin ph=H_L1_RES_ATTN_PROJ; pb=32'd128; end
    if (state == L1_NORM1_REQ) begin ph=H_L1_NORM1_W; pb=32'd128; end
    if (state == L1_KNORM_REQ) begin ph=H_L1_MLA_KNORM_W; pb=32'd256; end
    if (state == L1_RM_NORM_REQ) begin ph=H_L1_RES_MLP_NORM_W; pb=32'd128; end
    if (state == L1_RM_PROJ_REQ) begin ph=H_L1_RES_MLP_PROJ; pb=32'd128; end
    if (state == L1_NORM2_REQ) begin ph=H_L1_NORM2_W; pb=32'd128; end
    if (state == L1_BIAS_REQ) begin ph=H_L1_MOE_BIAS; pb=32'd16; end
    if (state == OUT_RM_NORM_REQ) begin ph=H_RES_OUT_NORM_W; pb=32'd128; end
    if (state == OUT_RM_PROJ_REQ) begin ph=H_RES_OUT_PROJ; pb=32'd128; end
    if (state == FINAL_NORM_REQ) begin ph=H_FINAL_NORM_W; pb=32'd128; end
    dynamic_qsz_load = 1'b0; dynamic_matrix_id = '0;
    if (state == L0_ROUTER_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd9; end
    if (state == SG_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd10; end
    if (state == SU_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd11; end
    if (state == SD_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd12; end
    if (state == E0G_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd13 + l0_selected0*3; end
    if (state == E0U_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd14 + l0_selected0*3; end
    if (state == E0D_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd15 + l0_selected0*3; end
    if (state == E1G_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd13 + l0_selected1*3; end
    if (state == E1U_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd14 + l0_selected1*3; end
    if (state == E1D_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd15 + l0_selected1*3; end
    if (state == L1_WC_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd37; end
    if (state == L1_WQC_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd38; end
    if (state == L1_WQR_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd39; end
    if (state == L1_WKC_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd40; end
    if (state == L1_WKR_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd41; end
    if (state == L1_WV_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd42; end
    if (state == L1_O_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd43; end
    if (state == L1_ROUTER_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd44; end
    if (state == L1_SG_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd45; end
    if (state == L1_SU_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd46; end
    if (state == L1_SD_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd47; end
    if (state == L1_E0G_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd48 + l1_selected0*3; end
    if (state == L1_E0U_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd49 + l1_selected0*3; end
    if (state == L1_E0D_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd50 + l1_selected0*3; end
    if (state == L1_E1G_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd48 + l1_selected1*3; end
    if (state == L1_E1U_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd49 + l1_selected1*3; end
    if (state == L1_E1D_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd50 + l1_selected1*3; end
    if (state == HEAD_REQ) begin dynamic_qsz_load=1'b1; dynamic_matrix_id=8'd72; end
    dynamic_meta = matrix_meta(dynamic_matrix_id, group_is_128);
    if (dynamic_qsz_load) begin
      qh=dynamic_meta.q_hash; qb=dynamic_meta.q_bytes; sh=dynamic_meta.s_hash; sb=dynamic_meta.s_bytes;
      zh=dynamic_meta.z_hash; zb=dynamic_meta.z_bytes;
      load_valid = 1'b1;
    end
    matvec_kind = 4'd0;  // KDA q/k/v/o are 64x64.
    if ((state == F1_REQ) || (state == F1_WAIT) || (state == F1_CAPTURE) ||
        (state == G1_REQ) || (state == G1_WAIT) || (state == G1_CAPTURE))
      matvec_kind = 4'd1;  // KDA f1/g1 are 64x32.
    if ((state == L0_ROUTER_REQ) || (state == L0_ROUTER_WAIT) || (state == L0_ROUTER_CAPTURE))
      matvec_kind = 4'd4;  // MoE router is 64x8.
    kda_step_valid = (state == KDA_STEP);
    l1_attn_start = (state == L1_ATTN_START);
    head_calc = head_g128;
    head_active = (state == HEAD_REQ) || (state == HEAD_WAIT) || (state == HEAD_CAPTURE);
    head_argmax_calc = '0;
    for (integer head_lane = 1; head_lane < 512; head_lane = head_lane + 1)
      if ($signed(head_calc[head_lane*DATA_W +: DATA_W]) > $signed(head_calc[head_argmax_calc*DATA_W +: DATA_W]))
        head_argmax_calc = head_lane[8:0];
    busy = !(state == IDLE || state == COMPLETE || state == FAILED);
    token_done = (state == COMPLETE); error = (state == FAILED);
    logits_bus = logits_reg; argmax_index = argmax_reg;
    l0_attn_prefix = '0;
    for (integer mix_lane = 0; mix_lane < 64; mix_lane = mix_lane + 1)
      l0_attn_prefix[mix_lane*DATA_W +: DATA_W] =
        $signed(x_bus[mix_lane*DATA_W +: DATA_W]) + $signed(kda_attention[mix_lane*DATA_W +: DATA_W]);
    l0_bias = '0;
    for (integer bias_lane = 0; bias_lane < 8; bias_lane = bias_lane + 1)
      l0_bias[bias_lane*DATA_W +: DATA_W] = $signed(l0_bias_q11[bias_lane*16 +: 16]) <<< 3;
    // L0 is an attn-residual block boundary: its running prefix restarts at
    // the KDA output. The layer result is therefore attention + MoE, not
    // embedding + attention + MoE.
    l0_moe_prefix = '0;
    for (integer moe_lane = 0; moe_lane < 64; moe_lane = moe_lane + 1)
      l0_moe_prefix[moe_lane*DATA_W +: DATA_W] =
        $signed(kda_attention[moe_lane*DATA_W +: DATA_W]) + $signed(l0_moe_out[moe_lane*DATA_W +: DATA_W]);
    l1_prefix = '0;
    for (integer l1_lane = 0; l1_lane < 64; l1_lane = l1_lane + 1)
      l1_prefix[l1_lane*DATA_W +: DATA_W] =
        $signed(l0_out[l1_lane*DATA_W +: DATA_W]) + $signed(l1_mla_attention[l1_lane*DATA_W +: DATA_W]);
    l1_out_next = '0;
    for (integer l1_out_lane = 0; l1_out_lane < 64; l1_out_lane = l1_out_lane + 1)
      l1_out_next[l1_out_lane*DATA_W +: DATA_W] =
        $signed(l1_prefix[l1_out_lane*DATA_W +: DATA_W]) + $signed(l1_moe_out[l1_out_lane*DATA_W +: DATA_W]);
    l1_bias = '0; l1_zero_expert = '0;
    for (integer l1_bias_lane = 0; l1_bias_lane < 8; l1_bias_lane = l1_bias_lane + 1)
      l1_bias[l1_bias_lane*DATA_W +: DATA_W] = $signed(l1_bias_q11[l1_bias_lane*16 +: 16]) <<< 3;
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; x_bus <= '0; h_norm_reg <= '0; q_pre <= '0; k_pre <= '0; v_pre <= '0;
      qpre_qsig <= '0; kpre_qsig <= '0; vpre_qsig <= '0;
      beta_reg <= '0; f_reg <= '0; z_reg <= '0; qconv_reg <= '0; kconv_reg <= '0;
      vconv_reg <= '0; kda_a_reg <= '0; kda_dtb_reg <= '0; kda_onorm_reg <= '0;
      kda_core_reg <= '0; kda_attention <= '0; l0_block <= '0; l0_out <= '0; l0_res_attn_norm <= '0;
      kda_qconv_dbg <= '0; kda_kconv_dbg <= '0; kda_vconv_dbg <= '0;
      kda_qact_dbg <= '0; kda_kact_dbg <= '0; kda_vact_dbg <= '0;
      kda_qnorm_dbg <= '0; kda_knorm_dbg <= '0; kda_alpha_dbg <= '0;
      kda_head_raw_dbg <= '0; kda_head_norm_dbg <= '0; kda_beta_dbg <= '0;
      l0_res_attn_proj <= '0; l0_norm2_offset <= '0; l0_res_mlp_norm <= '0; l0_res_mlp_proj <= '0; l0_bias_q11 <= '0;
      l0_router_pre <= '0; l0_selected0 <= '0; l0_selected1 <= '0;
      l1_res_attn_norm <= '0; l1_res_attn_proj <= '0; l1_norm1_offset <= '0;
      l1_res_mlp_norm <= '0; l1_res_mlp_proj <= '0; l1_norm2_offset <= '0;
      l1_bias_q11 <= '0; l1_router_pre_reg <= '0; l1_selected0 <= '0; l1_selected1 <= '0; l1_out <= '0;
      out_res_norm <= '0; out_res_proj <= '0; final_norm_offset <= '0; logits_reg <= '0; argmax_reg <= '0;
      l1_wc_pre <= '0; l1_knorm_offset <= '0; l1_qc_pre <= '0; l1_qr_pre <= '0; l1_kr_pre <= '0;
      l1_kc <= '0; l1_value <= '0; l1_mla_attention <= '0;
    end else case (state)
      IDLE: if (begin_token) state <= EMB_REQ;
      EMB_REQ: if (load_accept) state <= EMB_WAIT;
      EMB_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= EMB_CAPTURE;
      EMB_CAPTURE: begin x_bus <= embedding_bus; l0_block <= embedding_bus; state <= L0_NORM1_REQ; end
      L0_NORM1_REQ: if (load_accept) state <= L0_NORM1_WAIT;
      L0_NORM1_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L0_NORM1_CAPTURE;
      L0_NORM1_CAPTURE: begin h_norm_reg <= h_norm; state <= KDA_BEGIN; end
      KDA_BEGIN: state <= KQ_REQ;
      KQ_REQ: if (load_accept) state <= KQ_WAIT;
      KQ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KQ_CAPTURE;
      KQ_CAPTURE: begin q_pre <= matvec_y[64*DATA_W-1:0]; qpre_qsig <= q_bus[31:0]; state <= KK_REQ; end
      KK_REQ: if (load_accept) state <= KK_WAIT;
      KK_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KK_CAPTURE;
      KK_CAPTURE: begin k_pre <= matvec_y[64*DATA_W-1:0]; kpre_qsig <= q_bus[31:0]; state <= KV_REQ; end
      KV_REQ: if (load_accept) state <= KV_WAIT;
      KV_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KV_CAPTURE;
      KV_CAPTURE: begin v_pre <= matvec_y[64*DATA_W-1:0]; vpre_qsig <= q_bus[31:0]; state <= KB_REQ; end
      KB_REQ: if (load_accept) state <= KB_WAIT;
      KB_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KB_CAPTURE;
      KB_CAPTURE: begin beta_reg <= beta_pre; state <= F1_REQ; end
      F1_REQ: if (load_accept) state <= F1_WAIT;
      F1_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= F1_CAPTURE;
      F1_CAPTURE: begin f_mid <= matvec_y[32*DATA_W-1:0]; state <= F2_REQ; end
      F2_REQ: if (load_accept) state <= F2_WAIT;
      F2_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= F2_CAPTURE;
      F2_CAPTURE: begin f_reg <= f_calc; state <= G1_REQ; end
      G1_REQ: if (load_accept) state <= G1_WAIT;
      G1_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= G1_CAPTURE;
      G1_CAPTURE: begin g_mid <= matvec_y[32*DATA_W-1:0]; state <= G2_REQ; end
      G2_REQ: if (load_accept) state <= G2_WAIT;
      G2_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= G2_CAPTURE;
      G2_CAPTURE: begin z_reg <= z_calc; state <= QCONV_REQ; end
      QCONV_REQ: if (load_accept) state <= QCONV_WAIT;
      QCONV_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= QCONV_CAPTURE;
      QCONV_CAPTURE: begin qconv_reg <= p_bus[64*4*16-1:0]; state <= KCONV_REQ; end
      KCONV_REQ: if (load_accept) state <= KCONV_WAIT;
      KCONV_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KCONV_CAPTURE;
      KCONV_CAPTURE: begin kconv_reg <= p_bus[64*4*16-1:0]; state <= VCONV_REQ; end
      VCONV_REQ: if (load_accept) state <= VCONV_WAIT;
      VCONV_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= VCONV_CAPTURE;
      VCONV_CAPTURE: begin vconv_reg <= p_bus[64*4*16-1:0]; state <= KA_REQ; end
      KA_REQ: if (load_accept) state <= KA_WAIT;
      KA_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KA_CAPTURE;
      KA_CAPTURE: begin kda_a_reg <= p_bus[2*16-1:0]; state <= KDTB_REQ; end
      KDTB_REQ: if (load_accept) state <= KDTB_WAIT;
      KDTB_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KDTB_CAPTURE;
      KDTB_CAPTURE: begin kda_dtb_reg <= p_bus[64*16-1:0]; state <= KONORM_REQ; end
      KONORM_REQ: if (load_accept) state <= KONORM_WAIT;
      KONORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KONORM_CAPTURE;
      KONORM_CAPTURE: begin kda_onorm_reg <= p_bus[32*16-1:0]; state <= KDA_STEP; end
      // This pulse advances the KDA state and all three causal-conv histories
      // exactly once; their state remains resident until reset.
      KDA_STEP: begin
        kda_core_reg <= kda_core;
        kda_qconv_dbg <= kda_recurrence.q_conv; kda_kconv_dbg <= kda_recurrence.k_conv;
        kda_vconv_dbg <= kda_recurrence.v_conv; kda_qact_dbg <= kda_recurrence.q_act;
        kda_kact_dbg <= kda_recurrence.k_act; kda_vact_dbg <= kda_recurrence.v_act;
        kda_qnorm_dbg <= kda_recurrence.q_norm; kda_knorm_dbg <= kda_recurrence.k_norm;
        kda_alpha_dbg <= kda_recurrence.alpha_bus; kda_beta_dbg <= kda_recurrence.beta_bus;
        kda_head_raw_dbg <= kda_recurrence.head_raw >>> 4;
        kda_head_norm_dbg <= kda_recurrence.head_norm >>> 4;
        state <= KO_REQ;
      end
      KO_REQ: if (load_accept) state <= KO_WAIT;
      KO_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= KO_CAPTURE;
      // li=0 is a block boundary.  `mix_in` is empty for res_attn, so the
      // first residual mix is the MLP mix over [block snapshot, attention].
      KO_CAPTURE: begin kda_attention <= matvec_y[64*DATA_W-1:0]; state <= RM_NORM_REQ; end
      RA_NORM_REQ: if (load_accept) state <= RA_NORM_WAIT;
      RA_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= RA_NORM_CAPTURE;
      RA_NORM_CAPTURE: begin l0_res_attn_norm <= p_bus[64*16-1:0]; state <= RA_PROJ_REQ; end
      RA_PROJ_REQ: if (load_accept) state <= RA_PROJ_WAIT;
      RA_PROJ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= RA_PROJ_CAPTURE;
      RA_PROJ_CAPTURE: begin l0_res_attn_proj <= p_bus[64*16-1:0]; state <= RA_MIX; end
      // Candidate 0 is the saved L0 boundary; candidate 1 is the raw prefix
      // (embedding plus KDA attention), never a normalized surrogate.
      RA_MIX: state <= L0_NORM2_REQ;
      L0_NORM2_REQ: if (load_accept) state <= L0_NORM2_WAIT;
      L0_NORM2_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L0_NORM2_CAPTURE;
      L0_NORM2_CAPTURE: begin l0_norm2_offset <= p_bus[64*16-1:0]; state <= L0_BIAS_REQ; end
      L0_BIAS_REQ: if (load_accept) state <= L0_BIAS_WAIT;
      L0_BIAS_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L0_BIAS_CAPTURE;
      L0_BIAS_CAPTURE: begin l0_bias_q11 <= p_bus[8*16-1:0]; state <= L0_ROUTER_REQ; end
      L0_ROUTER_REQ: if (load_accept) state <= L0_ROUTER_WAIT;
      L0_ROUTER_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L0_ROUTER_CAPTURE;
      L0_ROUTER_CAPTURE: begin l0_router_pre <= matvec_y[8*DATA_W-1:0]; state <= L0_ROUTE_CAPTURE; end
      // The contract applies bias only to the stable top-2 selection.  Its
      // mixture weights remain derived from the un-biased sigmoid scores.
      L0_ROUTE_CAPTURE: begin l0_selected0 <= l0_route0; l0_selected1 <= l0_route1; state <= SG_REQ; end
      SG_REQ: if (load_accept) state <= SG_WAIT;
      SG_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= SG_CAPTURE;
      SG_CAPTURE: begin sgq<=q_bus[64/2*32*8-1:0]; sgs<=s_bus[32*16-1:0]; sgz<=z_bus[32*8-1:0]; state<=SU_REQ; end
      SU_REQ: if (load_accept) state <= SU_WAIT;
      SU_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= SU_CAPTURE;
      SU_CAPTURE: begin suq<=q_bus[64/2*32*8-1:0]; sus<=s_bus[32*16-1:0]; suz<=z_bus[32*8-1:0]; state<=SD_REQ; end
      SD_REQ: if (load_accept) state <= SD_WAIT;
      SD_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= SD_CAPTURE;
      SD_CAPTURE: begin sdq<=q_bus[32/2*64*8-1:0]; sds<=s_bus[64*16-1:0]; sdz<=z_bus[64*8-1:0]; state<=E0G_REQ; end
      E0G_REQ: if (load_accept) state <= E0G_WAIT;
      E0G_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E0G_CAPTURE;
      E0G_CAPTURE: begin e0gq<=q_bus[64/2*32*8-1:0]; e0gs<=s_bus[32*16-1:0]; e0gz<=z_bus[32*8-1:0]; state<=E0U_REQ; end
      E0U_REQ: if (load_accept) state <= E0U_WAIT;
      E0U_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E0U_CAPTURE;
      E0U_CAPTURE: begin e0uq<=q_bus[64/2*32*8-1:0]; e0us<=s_bus[32*16-1:0]; e0uz<=z_bus[32*8-1:0]; state<=E0D_REQ; end
      E0D_REQ: if (load_accept) state <= E0D_WAIT;
      E0D_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E0D_CAPTURE;
      E0D_CAPTURE: begin e0dq<=q_bus[32/2*64*8-1:0]; e0ds<=s_bus[64*16-1:0]; e0dz<=z_bus[64*8-1:0]; state<=E1G_REQ; end
      E1G_REQ: if (load_accept) state <= E1G_WAIT;
      E1G_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E1G_CAPTURE;
      E1G_CAPTURE: begin e1gq<=q_bus[64/2*32*8-1:0]; e1gs<=s_bus[32*16-1:0]; e1gz<=z_bus[32*8-1:0]; state<=E1U_REQ; end
      E1U_REQ: if (load_accept) state <= E1U_WAIT;
      E1U_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E1U_CAPTURE;
      E1U_CAPTURE: begin e1uq<=q_bus[64/2*32*8-1:0]; e1us<=s_bus[32*16-1:0]; e1uz<=z_bus[32*8-1:0]; state<=E1D_REQ; end
      E1D_REQ: if (load_accept) state <= E1D_WAIT;
      E1D_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= E1D_CAPTURE;
      E1D_CAPTURE: begin e1dq<=q_bus[32/2*64*8-1:0]; e1ds<=s_bus[64*16-1:0]; e1dz<=z_bus[64*8-1:0]; state<=L0_MOE_MIX; end
      L0_MOE_MIX: begin l0_out <= l0_moe_prefix; state <= L1_RA_NORM_REQ; end
      RM_NORM_REQ: if (load_accept) state <= RM_NORM_WAIT;
      RM_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= RM_NORM_CAPTURE;
      RM_NORM_CAPTURE: begin l0_res_mlp_norm <= p_bus[64*16-1:0]; state <= RM_PROJ_REQ; end
      RM_PROJ_REQ: if (load_accept) state <= RM_PROJ_WAIT;
      RM_PROJ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= RM_PROJ_CAPTURE;
      RM_PROJ_CAPTURE: begin l0_res_mlp_proj <= p_bus[64*16-1:0]; state <= RM_MIX; end
      RM_MIX: state <= L0_NORM2_REQ;
      // L1 is not a block boundary. Its attention residual candidates are
      // the saved L0 block snapshot and the complete L0 layer output.
      L1_RA_NORM_REQ: if (load_accept) state <= L1_RA_NORM_WAIT;
      L1_RA_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_RA_NORM_CAPTURE;
      L1_RA_NORM_CAPTURE: begin l1_res_attn_norm <= p_bus[64*16-1:0]; state <= L1_RA_PROJ_REQ; end
      L1_RA_PROJ_REQ: if (load_accept) state <= L1_RA_PROJ_WAIT;
      L1_RA_PROJ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_RA_PROJ_CAPTURE;
      L1_RA_PROJ_CAPTURE: begin l1_res_attn_proj <= p_bus[64*16-1:0]; state <= L1_RA_MIX; end
      L1_RA_MIX: state <= L1_NORM1_REQ;
      L1_NORM1_REQ: if (load_accept) state <= L1_NORM1_WAIT;
      L1_NORM1_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_NORM1_CAPTURE;
      L1_NORM1_CAPTURE: begin l1_norm1_offset <= p_bus[64*16-1:0]; state <= L1_WC_REQ; end
      L1_WC_REQ: if (load_accept) state <= L1_WC_WAIT;
      L1_WC_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WC_CAPTURE;
      L1_WC_CAPTURE: begin l1_wc_pre <= l1_wc_calc; state <= L1_KNORM_REQ; end
      L1_KNORM_REQ: if (load_accept) state <= L1_KNORM_WAIT;
      L1_KNORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_KNORM_CAPTURE;
      L1_KNORM_CAPTURE: begin l1_knorm_offset <= p_bus[128*16-1:0]; state <= L1_WQC_REQ; end
      L1_WQC_REQ: if (load_accept) state <= L1_WQC_WAIT;
      L1_WQC_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WQC_CAPTURE;
      L1_WQC_CAPTURE: begin l1_qc_pre <= l1_qc_calc; state <= L1_WQR_REQ; end
      L1_WQR_REQ: if (load_accept) state <= L1_WQR_WAIT;
      L1_WQR_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WQR_CAPTURE;
      L1_WQR_CAPTURE: begin l1_qr_pre <= l1_qr_calc; state <= L1_WKR_REQ; end
      L1_WKR_REQ: if (load_accept) state <= L1_WKR_WAIT;
      L1_WKR_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WKR_CAPTURE;
      L1_WKR_CAPTURE: begin l1_kr_pre <= l1_kr_calc; state <= L1_WKC_REQ; end
      L1_WKC_REQ: if (load_accept) state <= L1_WKC_WAIT;
      L1_WKC_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WKC_CAPTURE;
      L1_WKC_CAPTURE: begin l1_kc <= l1_kc_calc; state <= L1_WV_REQ; end
      L1_WV_REQ: if (load_accept) state <= L1_WV_WAIT;
      L1_WV_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_WV_CAPTURE;
      L1_WV_CAPTURE: begin l1_value <= l1_value_calc; state <= L1_ATTN_START; end
      L1_ATTN_START: state <= L1_ATTN_WAIT;
      L1_ATTN_WAIT: if (l1_attn_done) state <= L1_O_REQ;
      L1_O_REQ: if (load_accept) state <= L1_O_WAIT;
      L1_O_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_O_CAPTURE;
      L1_O_CAPTURE: begin l1_mla_attention <= l1_o_calc; state <= L1_RM_NORM_REQ; end
      // L1 is non-boundary: its residual prefix is L0.out + MLA attention.
      L1_RM_NORM_REQ: if (load_accept) state <= L1_RM_NORM_WAIT;
      L1_RM_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_RM_NORM_CAPTURE;
      L1_RM_NORM_CAPTURE: begin l1_res_mlp_norm <= p_bus[64*16-1:0]; state <= L1_RM_PROJ_REQ; end
      L1_RM_PROJ_REQ: if (load_accept) state <= L1_RM_PROJ_WAIT;
      L1_RM_PROJ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_RM_PROJ_CAPTURE;
      L1_RM_PROJ_CAPTURE: begin l1_res_mlp_proj <= p_bus[64*16-1:0]; state <= L1_RM_MIX; end
      L1_RM_MIX: state <= L1_NORM2_REQ;
      L1_NORM2_REQ: if (load_accept) state <= L1_NORM2_WAIT;
      L1_NORM2_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_NORM2_CAPTURE;
      L1_NORM2_CAPTURE: begin l1_norm2_offset <= p_bus[64*16-1:0]; state <= L1_BIAS_REQ; end
      L1_BIAS_REQ: if (load_accept) state <= L1_BIAS_WAIT;
      L1_BIAS_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_BIAS_CAPTURE;
      L1_BIAS_CAPTURE: begin l1_bias_q11 <= p_bus[8*16-1:0]; state <= L1_ROUTER_REQ; end
      L1_ROUTER_REQ: if (load_accept) state <= L1_ROUTER_WAIT;
      L1_ROUTER_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_ROUTER_CAPTURE;
      L1_ROUTER_CAPTURE: begin
        l1_router_pre_reg <= l1_router_pre;
        state <= L1_ROUTE_CAPTURE;
      end
      L1_ROUTE_CAPTURE: begin l1_selected0 <= l1_route0; l1_selected1 <= l1_route1; state <= L1_SG_REQ; end
      L1_SG_REQ: if (load_accept) state <= L1_SG_WAIT;
      L1_SG_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_SG_CAPTURE;
      L1_SG_CAPTURE: begin l1_sgq<=q_bus[64/2*32*8-1:0]; l1_sgs<=s_bus[32*16-1:0]; l1_sgz<=z_bus[32*8-1:0]; state<=L1_SU_REQ; end
      L1_SU_REQ: if (load_accept) state <= L1_SU_WAIT;
      L1_SU_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_SU_CAPTURE;
      L1_SU_CAPTURE: begin l1_suq<=q_bus[64/2*32*8-1:0]; l1_sus<=s_bus[32*16-1:0]; l1_suz<=z_bus[32*8-1:0]; state<=L1_SD_REQ; end
      L1_SD_REQ: if (load_accept) state <= L1_SD_WAIT;
      L1_SD_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_SD_CAPTURE;
      L1_SD_CAPTURE: begin l1_sdq<=q_bus[32/2*64*8-1:0]; l1_sds<=s_bus[64*16-1:0]; l1_sdz<=z_bus[64*8-1:0]; state<=L1_E0G_REQ; end
      L1_E0G_REQ: if (load_accept) state <= L1_E0G_WAIT;
      L1_E0G_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E0G_CAPTURE;
      L1_E0G_CAPTURE: begin l1_e0gq<=q_bus[64/2*32*8-1:0]; l1_e0gs<=s_bus[32*16-1:0]; l1_e0gz<=z_bus[32*8-1:0]; state<=L1_E0U_REQ; end
      L1_E0U_REQ: if (load_accept) state <= L1_E0U_WAIT;
      L1_E0U_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E0U_CAPTURE;
      L1_E0U_CAPTURE: begin l1_e0uq<=q_bus[64/2*32*8-1:0]; l1_e0us<=s_bus[32*16-1:0]; l1_e0uz<=z_bus[32*8-1:0]; state<=L1_E0D_REQ; end
      L1_E0D_REQ: if (load_accept) state <= L1_E0D_WAIT;
      L1_E0D_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E0D_CAPTURE;
      L1_E0D_CAPTURE: begin l1_e0dq<=q_bus[32/2*64*8-1:0]; l1_e0ds<=s_bus[64*16-1:0]; l1_e0dz<=z_bus[64*8-1:0]; state<=L1_E1G_REQ; end
      L1_E1G_REQ: if (load_accept) state <= L1_E1G_WAIT;
      L1_E1G_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E1G_CAPTURE;
      L1_E1G_CAPTURE: begin l1_e1gq<=q_bus[64/2*32*8-1:0]; l1_e1gs<=s_bus[32*16-1:0]; l1_e1gz<=z_bus[32*8-1:0]; state<=L1_E1U_REQ; end
      L1_E1U_REQ: if (load_accept) state <= L1_E1U_WAIT;
      L1_E1U_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E1U_CAPTURE;
      L1_E1U_CAPTURE: begin l1_e1uq<=q_bus[64/2*32*8-1:0]; l1_e1us<=s_bus[32*16-1:0]; l1_e1uz<=z_bus[32*8-1:0]; state<=L1_E1D_REQ; end
      L1_E1D_REQ: if (load_accept) state <= L1_E1D_WAIT;
      L1_E1D_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= L1_E1D_CAPTURE;
      L1_E1D_CAPTURE: begin l1_e1dq<=q_bus[32/2*64*8-1:0]; l1_e1ds<=s_bus[64*16-1:0]; l1_e1dz<=z_bus[64*8-1:0]; state<=L1_MOE_MIX; end
      L1_MOE_MIX: begin l1_out <= l1_out_next; state <= OUT_RM_NORM_REQ; end
      OUT_RM_NORM_REQ: if (load_accept) state <= OUT_RM_NORM_WAIT;
      OUT_RM_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= OUT_RM_NORM_CAPTURE;
      OUT_RM_NORM_CAPTURE: begin out_res_norm <= p_bus[64*16-1:0]; state <= OUT_RM_PROJ_REQ; end
      OUT_RM_PROJ_REQ: if (load_accept) state <= OUT_RM_PROJ_WAIT;
      OUT_RM_PROJ_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= OUT_RM_PROJ_CAPTURE;
      OUT_RM_PROJ_CAPTURE: begin out_res_proj <= p_bus[64*16-1:0]; state <= OUT_RM_MIX; end
      OUT_RM_MIX: state <= FINAL_NORM_REQ;
      FINAL_NORM_REQ: if (load_accept) state <= FINAL_NORM_WAIT;
      FINAL_NORM_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= FINAL_NORM_CAPTURE;
      FINAL_NORM_CAPTURE: begin final_norm_offset <= p_bus[64*16-1:0]; state <= HEAD_REQ; end
      HEAD_REQ: if (load_accept) state <= HEAD_WAIT;
      HEAD_WAIT: if (load_error) state <= FAILED; else if (load_done) state <= HEAD_CAPTURE;
      HEAD_CAPTURE: begin logits_reg <= head_calc; argmax_reg <= head_argmax_calc; state <= COMPLETE; end
      // Subsequent states are explicit control boundaries for the KDA/MLA/MoE
      // microprogram; they remain fail-closed until their full dependencies
      // are installed, rather than emitting non-golden logits.
      MLA_BEGIN, MOE_BEGIN, HEAD_BEGIN: state <= FAILED;
      COMPLETE, FAILED: state <= IDLE;
      default: state <= IDLE;
    endcase
  end
endmodule
