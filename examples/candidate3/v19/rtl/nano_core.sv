// ---------------------------------------------------------------------------
// Nano Decoder accelerator core: external-memory driven sequential decode.
//
// Storage macros (arch §4.2 plus a 16x1024 convolution kernel table):
//   a_m_qz 4096x320, a_m_scale 512x160, a_m_small 256x256, a_m_state 64x512,
//   a_m_conv 16x1024, a_m_convk 16x1024, a_m_kv_key 64x1536, a_m_kv_val
//   64x1024, a_m_logits 128x128.
// Datapath: 16x64x16 activation file with an 8-bit low plane (Q4.19 split
// value = hi*256 + lo), 64 MAC lanes (5-bit weight or 16x16 operands, 40-bit
// lane accumulators, plus a 24x16 wide dot for the Q4.19 stream), 48 ALU
// lanes with a 24-bit mode, 16 nonlinear lanes, one scalar SFU, one 64-lane
// reduction with a wide sum-of-squares output.
// All control is registered; every fixed-point writeback rounds to
// nearest-even and saturates (PLAN.md sections 2, 2b and 6).
// ---------------------------------------------------------------------------
`include "nano_desc.svh"

module nano_core (
  input  logic          clk,
  input  logic          rst_n,
  input  logic          cmd_valid,
  input  logic [31:0]   cmd_data,
  output logic          cmd_ready,
  output logic          rsp_valid,
  output logic [31:0]   rsp_data,
  input  logic          rsp_ready,
  output logic          mem_req_valid,
  output logic          mem_req_write,
  input  logic          mem_req_ready,
  output logic [35:0]   mem_req_addr,
  output logic [127:0]  mem_req_wdata,
  output logic [15:0]   mem_req_wstrb,
  input  logic          mem_rsp_valid,
  input  logic [127:0]  mem_rsp_rdata,
  output logic          mem_rsp_ready,
  output logic [31:0]   cyc_count,
  output logic          core_busy
);
  localparam [31:0] RUN_W = 32'h00000001, DONE_W = 32'h0000D0DE;

  // VRF slots
  localparam [3:0] S_A = 4'd0, S_B = 4'd1, S_X = 4'd2, S_X0 = 4'd3;
  localparam [3:0] S_P = 4'd4, S_T = 4'd5, S_Q = 4'd6, S_K = 4'd7;
  localparam [3:0] S_V = 4'd8, S_F = 4'd9, S_Z = 4'd10, S_G = 4'd11;
  localparam [3:0] S_H = 4'd12, S_M = 4'd13, S_C = 4'd14, S_D = 4'd15;

  // phases
  localparam [3:0] P_IDLE = 0, P_PRE = 1, P_LOAD = 2, P_TOKRD = 3, P_INIT = 4,
                   P_STEP = 5, P_DRAIN = 6, P_DONE = 7;

  // token program steps
  localparam [7:0]
    E0_EMB = 7'd1, E0_X0 = 7'd2, E0_N1L0 = 7'd3, E0_KQ = 7'd4, E0_KK = 7'd5,
    E0_KV = 7'd6, E0_KB = 7'd7, E0_KB2 = 7'd8, E0_KF = 7'd9, E0_KF2 = 7'd10,
    E0_KG = 7'd11, E0_KG2 = 7'd12, E0_ALPHA = 7'd13, E0_L2QK = 7'd14,
    E0_L2K = 7'd15, E0_ST0 = 7'd16, E0_ST1 = 7'd17, E0_KOUT = 7'd18,
    E0_SIGZ = 7'd19, E0_KO = 7'd20, E0_RM0A = 7'd21, E0_RM0B = 7'd22,
    E0_RM0B2 = 7'd23, E0_RM0B3 = 7'd24, E0_RM0C = 7'd25, E0_RM0D = 7'd26,
    E0_RM0D2 = 7'd27, E0_RM0E = 7'd28, E0_N2L0 = 7'd29, E0_MOE0 = 7'd30,
    E0_MF = 7'd31,
    E1_PRE = 7'd32, E1_RM1A = 7'd33, E1_RM1A2 = 7'd34, E1_RM1A3 = 7'd35,
    E1_RM1A4 = 7'd36, E1_RM1A5 = 7'd37, E1_RM1A6 = 7'd38, E1_RM1A7 = 7'd39,
    E1_RM1A8 = 7'd40, E1_N1L1 = 7'd41, E1_MLAC = 7'd42, E1_MLAC2 = 7'd43,
    E1_MLAQ = 7'd44, E1_MLAQ2 = 7'd45, E1_MLAK = 7'd46, E1_MLAK2 = 7'd47,
    E1_MLAV = 7'd48, E1_KVAPP = 7'd49, E1_ATT = 7'd50, E1_CTX = 7'd51,
    E1_PRE2 = 7'd52, E1_RM1B = 7'd53, E1_RM1B2 = 7'd54, E1_RM1B3 = 7'd55,
    E1_RM1B4 = 7'd56, E1_RM1B5 = 7'd57, E1_RM1B6 = 7'd58, E1_RM1B7 = 7'd59,
    E1_RM1B8 = 7'd60, E1_N2L1 = 7'd61, E1_MOE1 = 7'd62, E1_MF = 7'd63,
    E2_RM = 7'd64, E2_RM2 = 7'd65, E2_RM3 = 7'd66, E2_RM4 = 7'd67,
    E2_RM5 = 7'd68, E2_RM6 = 7'd69, E2_RM7 = 7'd70, E2_RM8 = 7'd71,
    E2_FN = 7'd72, E2_HEAD = 7'd73, E2_END = 7'd74,
    // shared executors
    X_NORM = 7'd90, X_NORMH = 7'd91, X_GEMV = 7'd92, X_GEMV2 = 7'd93,
    X_GEMVCONV = 7'd94, X_CONV = 7'd95, X_SML = 7'd96, X_ALU = 7'd97,
    X_RED = 7'd98, X_SFU = 7'd99, X_NL = 7'd100, X_MACD = 7'd101,
    X_MACC = 7'd102, X_MOE = 7'd103, X_RMIX = 7'd104, X_TOP2 = 7'd105,
    X_KVW = 7'd106, X_ALPHA = 7'd107, X_L2 = 7'd108, X_SIGMUL = 7'd109,
    X_STATE = 7'd110, X_ATTN = 7'd111, X_ADDV = 7'd112, X_DOT = 7'd113,
    // per-layer convolution and MoE continuation steps
    E0_KQ2 = 8'd128, E0_KK2 = 8'd129, E0_KV2 = 8'd130,
    E0_MOES = 8'd131, E0_MOSG = 8'd132, E0_MOS2 = 8'd133, E0_MOBI = 8'd134,
    E0_MOB2 = 8'd135, E0_MOTT = 8'd136,
    E0_MG0 = 8'd137, E0_MU0 = 8'd138, E0_MA0 = 8'd139, E0_MM0 = 8'd140,
    E0_MD0 = 8'd141, E0_MZ0 = 8'd142,
    E0_MG1 = 8'd143, E0_MU1 = 8'd144, E0_MA1 = 8'd145, E0_MM1 = 8'd146,
    E0_MD1 = 8'd147, E0_MZ1 = 8'd148,
    E0_MG2 = 8'd149, E0_MU2 = 8'd150, E0_MA2 = 8'd151, E0_MM2 = 8'd152,
    E0_MD2 = 8'd153, E0_MZ2 = 8'd154,
    E1_MOES = 8'd155, E1_MOSG = 8'd156, E1_MOS2 = 8'd157, E1_MOBI = 8'd158,
    E1_MOB2 = 8'd159, E1_MOTT = 8'd160,
    E1_MG0 = 8'd161, E1_MU0 = 8'd162, E1_MA0 = 8'd163, E1_MM0 = 8'd164,
    E1_MD0 = 8'd165, E1_MZ0 = 8'd166,
    E1_MG1 = 8'd167, E1_MU1 = 8'd168, E1_MA1 = 8'd169, E1_MM1 = 8'd170,
    E1_MD1 = 8'd171, E1_MZ1 = 8'd172,
    E1_MG2 = 8'd173, E1_MU2 = 8'd174, E1_MA2 = 8'd175, E1_MM2 = 8'd176,
    E1_MD2 = 8'd177, E1_MZ2 = 8'd178,
    X_MOEACC = 8'd179, X_SIG24 = 8'd180, X_SILU24 = 8'd181;

  // ---- storage ----
  logic [11:0] qz_ra; logic [319:0] qz_rd;
  logic qz_we; logic [11:0] qz_wa; logic [319:0] qz_wd;
  logic [8:0] sc_ra; logic [159:0] sc_rd;
  logic sc_we; logic [8:0] sc_wa; logic [159:0] sc_wd; logic [19:0] sc_wstrb;
  logic [7:0] sm_ra; logic [255:0] sm_rd;
  logic sm_we; logic [7:0] sm_wa; logic [255:0] sm_wd; logic [31:0] sm_wstrb;
  logic [5:0] st_ra; logic [767:0] st_rd;
  logic st_we; logic [5:0] st_wa; logic [767:0] st_wd;
  logic [3:0] cv_ra; logic [1023:0] cv_rd;
  logic cv_we; logic [3:0] cv_wa; logic [1023:0] cv_wd; logic [127:0] cv_wstrb;
  logic [3:0] ck_ra; logic [1023:0] ck_rd;
  logic ck_we; logic [3:0] ck_wa; logic [1023:0] ck_wd; logic [127:0] ck_wstrb;
  logic [5:0] kk_ra; logic [1535:0] kk_rd;
  logic kk_we; logic [5:0] kk_wa; logic [1535:0] kk_wd; logic [191:0] kk_wstrb;
  logic [5:0] vv_ra; logic [1023:0] vv_rd;
  logic vv_we; logic [5:0] vv_wa; logic [1023:0] vv_wd; logic [127:0] vv_wstrb;
  logic [6:0] lg_ra; logic [127:0] lg_rd;
  logic lg_we; logic [6:0] lg_wa; logic [127:0] lg_wd; logic [15:0] lg_wstrb;

  // Addresses actually presented to the synchronous read ports.  Every
  // executor is written for "address presented in cycle t -> data usable in
  // cycle t+1", so the address of the cycle the sequencer *sets* it must be
  // visible in that same cycle (a registered address would be one cycle late).
  logic [11:0] qz_ra_p; logic [8:0] sc_ra_p; logic [7:0] sm_ra_p;
  logic [3:0]  cv_ra_p, ck_ra_p; logic [5:0] kk_ra_p, vv_ra_p, st_ra_p;

  sram_1r1w #(4096, 320) u_qz (.clk(clk), .rd_en(1'b1), .rd_addr(qz_ra_p), .rd_data(qz_rd),
    .we(qz_we), .wr_addr(qz_wa), .wr_data(qz_wd), .wr_strb(40'hFFFFFFFFFF));
  sram_1r1w #(512, 160) u_sc (.clk(clk), .rd_en(1'b1), .rd_addr(sc_ra_p), .rd_data(sc_rd),
    .we(sc_we), .wr_addr(sc_wa), .wr_data(sc_wd), .wr_strb(sc_wstrb));
  sram_1r1w #(256, 256) u_sm (.clk(clk), .rd_en(1'b1), .rd_addr(sm_ra_p), .rd_data(sm_rd),
    .we(sm_we), .wr_addr(sm_wa), .wr_data(sm_wd), .wr_strb(sm_wstrb));
  // 64 rows x 768 bits: high plane bits [511:0] (32 x Q4.11) and low plane
  // bits [767:512] (32 x signed 8-bit remainder of the Q4.19 split state).
  sram_1r1w #(64, 768) u_state (.clk(clk), .rd_en(1'b1), .rd_addr(st_ra_p), .rd_data(st_rd),
    .we(st_we), .wr_addr(st_wa), .wr_data(st_wd), .wr_strb({96{1'b1}}));
  sram_1r1w #(16, 1024) u_conv (.clk(clk), .rd_en(1'b1), .rd_addr(cv_ra_p), .rd_data(cv_rd),
    .we(cv_we), .wr_addr(cv_wa), .wr_data(cv_wd), .wr_strb(cv_wstrb));
  sram_1r1w #(16, 1024) u_convk (.clk(clk), .rd_en(1'b1), .rd_addr(ck_ra_p), .rd_data(ck_rd),
    .we(ck_we), .wr_addr(ck_wa), .wr_data(ck_wd), .wr_strb(ck_wstrb));
  sram_1r1w #(64, 1536) u_kvk (.clk(clk), .rd_en(1'b1), .rd_addr(kk_ra_p), .rd_data(kk_rd),
    .we(kk_we), .wr_addr(kk_wa), .wr_data(kk_wd), .wr_strb(kk_wstrb));
  sram_1r1w #(64, 1024) u_kvv (.clk(clk), .rd_en(1'b1), .rd_addr(vv_ra_p), .rd_data(vv_rd),
    .we(vv_we), .wr_addr(vv_wa), .wr_data(vv_wd), .wr_strb(vv_wstrb));
  // Logits macro read enable.  The drain presents the address one beat ahead
  // so that one beat per cycle is possible, which means the port data must be
  // *held* while a posted write is backpressured: otherwise a stalled beat
  // would age to the next word.  The macro holds its output when not read.
  logic lg_rd_en;
  assign lg_rd_en = (phase != P_DRAIN) || wr_ready;

  sram_1r1w #(128, 128) u_log (.clk(clk), .rd_en(lg_rd_en), .rd_addr(lg_ra), .rd_data(lg_rd),
    .we(lg_we), .wr_addr(lg_wa), .wr_data(lg_wd), .wr_strb(lg_wstrb));

  // ---- register file + units ----
  logic [3:0] vrf_ra, vrf_rb, vrf_rc, vrf_rd, vrf_wa;
  logic [1023:0] vrf_va, vrf_vb, vrf_vc, vrf_vd, vrf_wd;
  logic [511:0]  vrf_lva, vrf_lvb, vrf_lvc, vrf_lvd, vrf_lwd;
  logic vrf_we;
  vrf #(16, 64, 4) u_vrf (.clk(clk), .ra(vrf_ra), .rb(vrf_rb), .rc(vrf_rc), .rd(vrf_rd),
    .va(vrf_va), .vb(vrf_vb), .vc(vrf_vc), .vd(vrf_vd),
    .la(vrf_lva), .lb(vrf_lvb), .lc(vrf_lvc), .ld(vrf_lvd),
    .we(vrf_we), .wa(vrf_wa), .wd(vrf_wd), .wd_lo(vrf_lwd));

  logic [1023:0] mac_opa, mac_opb;
  logic [511:0]  mac_opa_lo, mac_opb_lo;
  logic [23:0] mac_scl; logic [5:0] mac_sh; logic mac_clr, mac_en, mac_wmode;
  logic [64*56-1:0] mac_acc; logic [1023:0] mac_acc_out;
  logic [64*24-1:0] mac_acc_wout;
  logic signed [47:0] mac_dot, mac_dot_lo;
  logic signed [55:0] mac_dot_wide;
  logic [6:0] oa_off, ob_off, ow_off;
  logic [2:0] oa_w, ob_w, ow_w;
  logic [3:0] oa_src, ob_src; logic [6:0] ob_blane;

  function automatic [6:0] wsel (input logic [2:0] w);
    wsel = (w == 3'd0) ? 7'd16 : (w == 3'd1) ? 7'd32 : (w == 3'd2) ? 7'd48 : 7'd64;
  endfunction

  // weight-stream section sizes (tools/pack_image.py): 1970 raw int16 small
  // parameters = 3940 bytes, then the tap-major convolution tail section.
  localparam int SMALL_SECTION_BYTES = 3940;

  // MAC operand mux.  Source order (both operands):
  //   0 vrf  1 st_rd  2 kk_rd  3 vv_rd  4 cv_rd  5 ck_rd  6 qz (5-bit signed,
  //   only as opb)  7 (opb only) broadcast of the single vrf_vb lane `ob_blane`.
  // `oa_off`/`ob_off` are element (not byte) offsets into the source row.
  // The low plane exists for the VRF and for the 24-bit Q4.19 split state; the
  // state high plane is bits [16*i +: 16] and its low plane [512 + 8*i +: 8].
  always_comb begin
    logic [6:0] w1, w2;
    w1 = wsel(oa_w);
    w2 = wsel(ob_w);
    for (int i = 0; i < 64; i++) begin
      case (oa_src)
        4'd1: mac_opa[i*16 +: 16] = (i < w1) ? st_rd[16*(oa_off + i) +: 16] : 16'sd0;
        4'd2: mac_opa[i*16 +: 16] = (i < w1) ? kk_rd[16*(oa_off + i) +: 16] : 16'sd0;
        4'd3: mac_opa[i*16 +: 16] = (i < w1) ? vv_rd[16*(oa_off + i) +: 16] : 16'sd0;
        4'd4: mac_opa[i*16 +: 16] = (i < w1) ? cv_rd[16*(oa_off + i) +: 16] : 16'sd0;
        4'd5: mac_opa[i*16 +: 16] = (i < w1) ? ck_rd[16*(oa_off + i) +: 16] : 16'sd0;
        default: mac_opa[i*16 +: 16] = (i < w1) ? vrf_va[16*(oa_off + i) +: 16] : 16'sd0;
      endcase
      case (ob_src)
        4'd1: mac_opb[i*16 +: 16] = (i < w2) ? st_rd[16*(ob_off + i) +: 16] : 16'sd0;
        4'd2: mac_opb[i*16 +: 16] = (i < w2) ? kk_rd[16*(ob_off + i) +: 16] : 16'sd0;
        4'd3: mac_opb[i*16 +: 16] = (i < w2) ? vv_rd[16*(ob_off + i) +: 16] : 16'sd0;
        4'd4: mac_opb[i*16 +: 16] = (i < w2) ? cv_rd[16*(ob_off + i) +: 16] : 16'sd0;
        4'd5: mac_opb[i*16 +: 16] = (i < w2) ? ck_rd[16*(ob_off + i) +: 16] : 16'sd0;
        4'd6: mac_opb[i*16 +: 16] = ((i + ob_off) < 64)
              ? {{11{qz_rd[(((i + ob_off) & 63))*5 + 4]}}, qz_rd[(((i + ob_off) & 63))*5 +: 5]}
              : 16'sd0;
        4'd7: mac_opb[i*16 +: 16] = vrf_vb[16*ob_blane +: 16];
        default: mac_opb[i*16 +: 16] = (i < w2) ? vrf_vb[16*(ob_off + i) +: 16] : 16'sd0;
      endcase
      if (oa_src == 4'd0 && i < w1)
        mac_opa_lo[i*8 +: 8] = vrf_lva[8*(oa_off + i) +: 8];
      else if (oa_src == 4'd1 && i < w1)
        mac_opa_lo[i*8 +: 8] = st_rd[512 + 8*((oa_off + i) & 7'd31) +: 8];
      else
        mac_opa_lo[i*8 +: 8] = 8'd0;
      if (ob_src == 4'd0 && i < w2)
        mac_opb_lo[i*8 +: 8] = vrf_lvb[8*(ob_off + i) +: 8];
      else if (ob_src == 4'd1 && i < w2)
        mac_opb_lo[i*8 +: 8] = st_rd[512 + 8*((ob_off + i) & 7'd31) +: 8];
      else
        mac_opb_lo[i*8 +: 8] = 8'd0;
    end
  end

  mac64 u_mac (.clk(clk), .clr(mac_clr), .en(mac_en), .opa(mac_opa), .opb(mac_opb),
    .opa_lo(mac_opa_lo), .wmode(mac_wmode), .scl(mac_scl), .sh(mac_sh),
    .dot_full(mac_dot), .dot_lo(mac_dot_lo), .dot_wide(mac_dot_wide),
    .acc(mac_acc), .acc_out(mac_acc_out), .acc_wout(mac_acc_wout));

  logic [2:0] alu_op; logic [4:0] alu_sh; logic signed [15:0] alu_s;
  // Scalar operand of the ALU can also be taken combinationally from a lane of
  // VRF read port D (`alu_soff`).  The KDA state recurrence needs one scalar per
  // row (alpha[row], (beta*k)[row]); port D holds the whole vector, so no extra
  // pipeline stage is needed between the row address and the ALU result.
  logic signed [15:0] alu_s_mux;
  logic [6:0] alu_soff; logic alu_s_vd;
  assign alu_s_mux = alu_s_vd ? $signed(vrf_vd[16*alu_soff[5:0] +: 16]) : alu_s;
  logic [48*16-1:0] alu_a, alu_b, alu_y;
  logic [48*8-1:0]  alu_a_lo, alu_b_lo, alu_y_lo;
  logic alu_wide, alu_b24;
  logic [6:0] alu_aoff, alu_boff; logic [2:0] alu_w;
  logic [2:0] alu_a_src, alu_b_src;      // same source order as the MAC mux
  always_comb begin
    logic [6:0] w;
    w = wsel(alu_w);
    for (int i = 0; i < 48; i++) begin
      case (alu_a_src)
        3'd1: alu_a[i*16 +: 16] = (i < w) ? st_rd[16*(alu_aoff + i) +: 16] : 16'sd0;
        3'd2: alu_a[i*16 +: 16] = (i < w) ? kk_rd[16*(alu_aoff + i) +: 16] : 16'sd0;
        3'd3: alu_a[i*16 +: 16] = (i < w) ? vv_rd[16*(alu_aoff + i) +: 16] : 16'sd0;
        3'd4: alu_a[i*16 +: 16] = (i < w) ? cv_rd[16*(alu_aoff + i) +: 16] : 16'sd0;
        3'd5: alu_a[i*16 +: 16] = (i < w) ? ck_rd[16*(alu_aoff + i) +: 16] : 16'sd0;
        default: alu_a[i*16 +: 16] = (i < w) ? vrf_va[16*(alu_aoff + i) +: 16] : 16'sd0;
      endcase
      case (alu_b_src)
        3'd1: alu_b[i*16 +: 16] = (i < w) ? st_rd[16*(alu_boff + i) +: 16] : 16'sd0;
        3'd2: alu_b[i*16 +: 16] = (i < w) ? kk_rd[16*(alu_boff + i) +: 16] : 16'sd0;
        3'd3: alu_b[i*16 +: 16] = (i < w) ? vv_rd[16*(alu_boff + i) +: 16] : 16'sd0;
        3'd4: alu_b[i*16 +: 16] = (i < w) ? cv_rd[16*(alu_boff + i) +: 16] : 16'sd0;
        3'd5: alu_b[i*16 +: 16] = (i < w) ? ck_rd[16*(alu_boff + i) +: 16] : 16'sd0;
        default: alu_b[i*16 +: 16] = (i < w) ? vrf_vb[16*(alu_boff + i) +: 16] : 16'sd0;
      endcase
      if (alu_a_src == 3'd0 && i < w)
        alu_a_lo[i*8 +: 8] = vrf_lva[8*(alu_aoff + i) +: 8];
      else if (alu_a_src == 3'd1 && i < w)
        alu_a_lo[i*8 +: 8] = st_rd[512 + 8*((alu_aoff + i) & 7'd31) +: 8];
      else
        alu_a_lo[i*8 +: 8] = 8'd0;
      if (alu_b_src == 3'd0 && i < w)
        alu_b_lo[i*8 +: 8] = vrf_lvb[8*(alu_boff + i) +: 8];
      else if (alu_b_src == 3'd1 && i < w)
        alu_b_lo[i*8 +: 8] = st_rd[512 + 8*((alu_boff + i) & 7'd31) +: 8];
      else
        alu_b_lo[i*8 +: 8] = 8'd0;
    end
  end
  alu48 u_alu (.op(alu_op), .sh(alu_sh), .s(alu_s_mux),
    .a(alu_a), .b(alu_b), .a_lo(alu_a_lo), .b_lo(alu_b_lo), .wide(alu_wide),
    .mul24(alu_b24), .y(alu_y), .y_lo(alu_y_lo));

  logic [1:0] nl_op; logic [4:0] nl_osh; logic [5:0] nl_off;
  logic [16*16-1:0] nl_a, nl_y;
  always_comb for (int i = 0; i < 16; i++) nl_a[i*16 +: 16] = vrf_va[(nl_off*16 + i*16) +: 16];
  nlin16 u_nl (.op(nl_op), .osh(nl_osh), .a(nl_a), .y(nl_y));

  logic [1:0] sfu_op; logic [47:0] sfu_din, sfu_din2, sfu_dout; logic [5:0] sfu_sh;
  sfu u_sfu (.op(sfu_op), .din(sfu_din), .din2(sfu_din2), .sh2(sfu_sh), .dout(sfu_dout));

  logic [1:0] red_op; logic signed [47:0] red_y;
  logic signed [55:0] red_sq;
  logic [1:0] red_win;                 // 0 = all 64 lanes, 1 = lanes 0..31, 2 = 32..63
  logic [1023:0] red_a;
  logic [511:0] red_a_lo;
  always_comb begin
    red_a = (red_win == 2'd1) ? {512'd0, vrf_va[511:0]}
          : (red_win == 2'd2) ? {512'd0, vrf_va[1023:512]} : vrf_va;
    red_a_lo = (red_win == 2'd1) ? {256'd0, vrf_lva[255:0]}
             : (red_win == 2'd2) ? {256'd0, vrf_lva[511:256]} : vrf_lva;
  end
  red64 u_red (.op(red_op), .a(red_a), .a_lo(red_a_lo), .r(red_y), .sq_wide(red_sq));

  // ---- memory interface ----
  logic rd_issue, rd_ready, fifo_valid, fifo_pop, wr_issue, wr_ready;
  logic [35:0] rd_addr, wr_addr;
  logic [127:0] fifo_data, wr_data;
  logic [15:0] wr_strb;

  // The read-request address is combinational from the beat counter so that a
  // request presented and accepted in the same cycle always carries that beat's
  // address.  A registered address lags the counter by one cycle once the
  // request path itself is combinational, which duplicates beat R and drops
  // the last beat of the stream.
  always_comb begin
    if (phase == P_TOKRD) rd_addr = cfg_tbase + 36'(36'(ld_req) * 36'd16);
    else                  rd_addr = cfg_wbase + 36'(36'(ld_req) * 36'd16);
  end

  // The read-request strobe is combinational from the phase and the beat
  // counter.  A registered strobe outlives its own start condition by one
  // cycle and issues one beat beyond the stream end.
  assign rd_issue = (ld_req < ld_nb) &&
                    ((phase == P_LOAD) ? (ld_out < 32'd48) :
                     (phase == P_TOKRD) && (sub == 6'd1));
  nano_mem #(64, 6) u_mem (.clk(clk), .rst_n(rst_n),
    .rd_issue(rd_issue), .rd_addr(rd_addr), .rd_ready(rd_ready),
    .fifo_valid(fifo_valid), .fifo_pop(fifo_pop), .fifo_data(fifo_data),
    .wr_issue(wr_issue), .wr_addr(wr_addr), .wr_data(wr_data), .wr_strb(wr_strb),
    .wr_ready(wr_ready),
    .mem_req_valid(mem_req_valid), .mem_req_write(mem_req_write),
    .mem_req_ready(mem_req_ready), .mem_req_addr(mem_req_addr),
    .mem_req_wdata(mem_req_wdata), .mem_req_wstrb(mem_req_wstrb),
    .mem_rsp_valid(mem_rsp_valid), .mem_rsp_rdata(mem_rsp_rdata),
    .mem_rsp_ready(mem_rsp_ready));

  // ---- configuration + sequencer state ----
  logic [5:0] cfg_seq_m8; logic cfg_grp64;
  logic [35:0] cfg_wbase, cfg_tbase, cfg_obase;
  logic [3:0] phase;
  logic [6:0] seq_len, tok;
  logic [7:0] step; logic [6:0] sub;
  logic [31:0] cycles;
  logic busy, rsp_hold;
  logic [6:0] cnt, cnt2;
  logic [15:0] drain_beat;
  logic [7:0] init_cnt;

  logic signed [47:0] red_lat, dot_lat, dot2_lat, ss_lat;
  logic [31:0] rm_ssum;                 // sum of the two res_mix exponentials
  logic signed [15:0] beta0, beta1, sc_q, sc_q2, w0q, w1q, sg_lat, sg_lat2;
  logic [23:0] rsc; logic [5:0] rsh;
  logic [2:0] sel0, sel1;
  logic signed [15:0] sc_buf [0:127];
  logic signed [47:0] csum0, csum1;
  logic signed [31:0] logit0, logit1, maxlog0, maxlog1;
  logic signed [31:0] argv;
  logic [8:0] argi;
  logic [31:0] tok_buf [0:63];
  logic [2:0] cv_base [0:3];
  logic [7:0] cvrow;              // row index inside the KDA state walk
  logic [6:0] cpos;               // attention position cursor
  logic [3:0] chead;
  logic signed [47:0] err_lat [0:63];
  logic signed [47:0] kb_lat [0:63];

  // gemv engine state
  logic gv_wide;
  logic gv_busy;
  logic [6:0] gv_mat; logic [3:0] gv_slo, gv_shi, gv_dst; logic gv_tolog;
  logic [12:0] gv_word, gv_nwords, gv_qzb;
  logic [13:0] gv_scb;
  logic [9:0] gv_n; logic [8:0] gv_K;
  logic [1:0] gv_mode, gv_ph;
  logic [3:0] gv_dl;
  logic signed [47:0] gv_colacc;
  logic [12:0] gv_dw;

  // ---- executor scratch state (driven by nano_exec.svh) ----
  logic [9:0] gv_col;                 // X_GEMV column cursor
  logic [1:0] gv_kb;                  // X_GEMV k-block cursor
  logic [9:0] pv_col;                 // GEMV word whose data is consumed now
  logic [1:0] pv_kb;                  //   (address was presented last cycle)
  logic signed [55:0] gv_acc;         // wide column accumulator (Q4.19 products)
  logic [1023:0] gv_row;              // GEMV result shadow row (16-bit plane)
  logic [511:0]  gv_rowl;             // GEMV result shadow row (8-bit plane)
  logic [1023:0] gv_rowh;             // high-half shadow row (columns 64..127)
  logic [511:0]  gv_rowhl;            // high-half low plane
  logic signed [47:0] gv_dot;         // latched dot product
  logic signed [15:0] gv_sc;          // latched group scale
  logic signed [31:0] gv_code;        // logit code
  logic signed [31:0] gv_max;         // running logit maximum
  logic [8:0] gv_argi;                // running argmax index
  logic [7:0] nm_blk;                 // X_NORM block cursor
  logic signed [55:0] nm_ss;          // Q4.19 sum of squares (Q4.38)
  logic [47:0] nm_m;                  // mean + eps
  logic [47:0] nm_g;                  // rsqrt result (Q1.14)
  logic signed [15:0] al_a0, al_a1;   // KDA per-head gate slopes (Q8.8)
  logic [7:0] st_row;                 // X_STATE row cursor
  logic signed [15:0] st_beta;        // KDA per-head beta (Q1.15)
  logic [7:0] at_pos;                 // attention position cursor
  logic signed [47:0] at_dot;         // raw attention score
  logic signed [47:0] at_tot;         // softmax denominator
  logic [7:0] at_ph;                  // attention sub-phase
  logic [2:0] cn_ph;                  // convolution sub-phase
  logic [2:0] mz_sel0, mz_sel1;       // MoE top-2 expert ids
  logic signed [23:0] mz_s0, mz_s1;   // pre-bias router scores (Q5.18)
  logic signed [15:0] ex_w;           // current expert route weight (Q2.13)
  logic signed [63:0] ex_p;           // 64-bit product scratch
  logic [7:0] ex_ph;                  // expert loop phase
  logic sd_ph;                        // sigmoid-multiply phase
  logic signed [15:0] at_mx;          // attention softmax maximum
  logic signed [15:0] at_r;           // scaled attention score
  logic [47:0] sfu_r;                 // sfu result register
  logic signed [15:0] mz_w0, mz_w1;   // routed-expert weights (Q2.13)
  logic [1:0] hist_ptr [0:2];         // rotating KDA conv history rows
  logic [7:0] nm_i;                   // 16-lane group cursor (X_NORM / X_L2)
  logic [4:0] nm_drop;                // gain drop of the magnitude-normalised rsqrt

  // loader state
  logic [2:0] ld_st;
  logic [6:0] ld_mat;
  logic [9:0] ld_n; logic [8:0] ld_t; logic [7:0] ld_r;
  logic [9:0] ld_cnt;
  logic [3:0] ld_bidx; logic ld_beat_valid; logic [127:0] ld_beat;
  logic [31:0] ld_total, ld_out;
  logic [15:0] ld_req, ld_nb;          // 16-bit beat counters (stream > 8 k beats)
  logic [31:0] ld_bytecnt;           // bytes consumed in the current stream
  logic [7:0] ld_zbuf [0:511];
  logic [319:0] ld_stage;
  logic [6:0] ld_lane;
  logic [12:0] ld_qzw;
  logic [3:0] ld_sph;
  logic [7:0] ld_lo;
  logic [5:0] ld_smid; logic [9:0] ld_smcnt; logic [1:0] ld_smph;
  logic [7:0] sm_word;

  // ---------------------------------------------------------------------------
  // Byte window of the weight-stream loader.
  // The 128-bit read beat is consumed as a byte window rather than one byte per
  // cycle: every cycle the loader consumes `ld_c` bytes starting at `ld_off`
  // inside `ld_src`.  `ld_src` is the held beat register, or the FIFO head --
  // read combinationally in nano_mem -- so a beat can be popped and consumed in
  // the same cycle.  Every destination block therefore advances at the external
  // read rate (one beat per cycle) instead of one byte per cycle:
  //   z   up to 16 bytes, s up to 8 int16 scale slots (one 160-bit word),
  //   q   up to 32 q-z lanes (half a 320-bit word), small up to 8 slots,
  //   conv up to 8 kernel slots.
  // Every stream block size is even (all descriptors have even N) and every
  // block starts at an even stream offset, so a pair-based group is never left
  // with a single byte before a beat boundary.
  // ---------------------------------------------------------------------------
  logic [4:0]   ld_c, ld_nv, ld_off;
  logic [127:0] ld_src;
  logic         ld_more;
  logic         fifo_pop_c;

  assign ld_more = ld_bytecnt < {ld_nb, 4'b0};
  assign ld_src  = ld_beat_valid ? ld_beat : fifo_data;
  assign ld_off  = ld_beat_valid ? {1'b0, ld_bidx} : 5'd0;
  assign ld_nv   = (ld_beat_valid || fifo_valid) ? (5'd16 - ld_off) : 5'd0;

  // One token beat carries four uint32 token ids, so the token phase consumes a
  // whole beat per cycle.  (A byte-serial walk serialized the beat and made the
  // phase's exit condition depend on the beat cursor.)
  always_comb begin
    fifo_pop_c = 1'b0;
    if (phase == P_LOAD) begin
      fifo_pop_c = (ld_c != 5'd0) && !ld_beat_valid && fifo_valid &&
                   ((ld_st != 3'd5) || ld_more);
    end else if (phase == P_TOKRD && sub == 6'd1) begin
      fifo_pop_c = fifo_valid && (cnt < ld_nb[6:0]);
    end
  end
  assign fifo_pop = fifo_pop_c;

  // Bytes consumed this cycle.  Each group is capped by the destination word,
  // the current column/row, and the bytes left in the beat.
  always_comb begin
    int rem, fit, cap, sl0, lane, room;
    rem = 0; fit = 0; cap = 0; sl0 = 0; lane = 0; room = 0;
    ld_c = 5'd0;
    if (phase == P_LOAD) begin
      case (ld_st)
        3'd0: begin                                    // z block, R*N bytes
          rem = int'(mR) * int'(mN) - int'(ld_cnt);
          fit = (rem > int'(ld_nv)) ? int'(ld_nv) : rem;
          fit = (fit > 16) ? 16 : fit;
          ld_c = 5'(fit);
        end
        3'd1: begin                                    // s block, 2 bytes/scale
          sl0 = int'(mscb) + int'(ld_n) * int'(mper) + int'(ld_r);
          cap = 10 - (sl0 % 10);                       // slots left in this word
          fit = (mper == 2'd1) ? cap : ((cap + 1) / 2);
          rem = int'(mN) - int'(ld_n);                 // columns left in this row
          fit = (fit > rem) ? rem : fit;
          room = int'(ld_nv) / 2;
          fit = (fit > room) ? room : fit;
          fit = (fit > 8) ? 8 : fit;
          ld_c = 5'(2 * fit);
        end
        3'd2: begin                                    // q block, 2 bytes/2 lanes
          rem = int'(mK) / 2 - int'(ld_t);             // pairs left in this column
          if (mK == 9'd128 && int'(ld_t) < 32) begin
            room = 32 - int'(ld_t);                    // zero-point row changes here
            rem = (rem > room) ? room : rem;
          end
          room = (64 - int'(ld_lane)) / 2;             // lane pairs left in this word
          fit = (rem > room) ? room : rem;
          room = int'(ld_nv);                          // one byte carries one pair
          fit = (fit > room) ? room : fit;
          ld_c = 5'(fit);
        end
        3'd3: begin                                    // small parameters
          sl0 = int'(SM_OFF_V[ld_smid*16 +: 16]) + int'(ld_smcnt);
          lane = sl0 % 16;                             // slots left in this word
          fit = 16 - lane;
          rem = int'(SM_LEN_V[ld_smid*12 +: 12]) - int'(ld_smcnt);
          fit = (fit > rem) ? rem : fit;
          room = int'(ld_nv) / 2;
          fit = (fit > room) ? room : fit;
          fit = (fit > 8) ? 8 : fit;
          ld_c = 5'(2 * fit);
        end
        3'd4: begin                                    // convolution kernels
          lane = int'(ld_smcnt) % 64;
          fit = 64 - lane;
          rem = int'(CONV_VALUES) - int'(ld_smcnt);
          fit = (fit > rem) ? rem : fit;
          room = int'(ld_nv) / 2;
          fit = (fit > room) ? room : fit;
          fit = (fit > 8) ? 8 : fit;
          ld_c = 5'(2 * fit);
        end
        default: ld_c = (ld_more && ld_nv != 5'd0) ? ld_nv : 5'd0;   // pad drain
      endcase
    end
  end

  // byte `idx` of the current window (idx < ld_nv)
  function automatic logic [7:0] ldbyte (input logic [127:0] src,
                                         input logic [4:0] off, input int idx);
    ldbyte = src[8*((off + 5'(idx)) & 5'd15) +: 8];
  endfunction

  logic [13:0] ld_slot;
  logic [12:0] ld_smoff9;

  // helper functions
  function automatic logic signed [15:0] rne16 (input logic signed [47:0] p, input logic [5:0] sh);
    logic signed [47:0] q, rem, half; logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((48'sd1 << sh) - 48'sd1);
      half = 48'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 48'sd1 : 48'sd0);
    end
    if (q > 48'sd32767) rne16 = 16'sh7FFF;
    else if (q < -48'sd32768) rne16 = 16'sh8000;
    else rne16 = q[15:0];
  endfunction

  function automatic logic signed [31:0] rne32 (input logic signed [63:0] p, input logic [5:0] sh);
    logic signed [63:0] q, rem, half; logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((64'sd1 << sh) - 64'sd1);
      half = 64'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 64'sd1 : 64'sd0);
    end
    if (q > 64'sd2147483647) rne32 = 32'sh7FFFFFFF;
    else if (q < -64'sd2147483648) rne32 = 32'sh80000000;
    else rne32 = q[31:0];
  endfunction

  // lane-range merge used by every partial VRF writeback
  function automatic logic [1023:0] mergev (input logic [1023:0] base,
                                            input logic [1023:0] src,
                                            input logic [6:0]    off,
                                            input logic [6:0]    wid);
    logic [63:0] lm;
    logic [1023:0] m;
    // lane-window merge as a mask: lane i takes src iff off <= i < off+wid.
    // The 64-bit lane mask saturates exactly like the loop bound: wid>=64
    // selects every lane from off, off>=64 selects none, and off+wid>64 clips
    // at lane 63. Each lane select is replicated over the lane's 16 bits, so
    // the plane is (base & ~m) | (src & m) with one uniform select per lane.
    lm = ((64'd1 << wid) - 64'd1) << off;
    for (int i = 0; i < 64; i++) m[i*16 +: 16] = {16{lm[i]}};
    mergev = (base & ~m) | (src & m);
  endfunction

  // 2x with signed 16-bit saturation: the KDA conv/SiLU outputs are stored
  // as Q4.12 (range +/-8) while the pre-activation arithmetic is Q4.11.
  function automatic logic [255:0] sat2x (input logic [255:0] v);
    logic signed [16:0] w;
    for (int i = 0; i < 16; i++) begin
      w = $signed(v[i*16 +: 16]);
      if (w > 17'sd16383)       sat2x[i*16 +: 16] = 16'sh7FFF;
      else if (w < -17'sd16384) sat2x[i*16 +: 16] = 16'sh8000;
      else                      sat2x[i*16 +: 16] = w <<< 1;
    end
  endfunction

  // nearest-even rescale of a 64-bit product to a saturated 16-bit code
  function automatic logic signed [15:0] rne16q (input logic signed [63:0] p,
                                                 input logic [5:0] sh);
    logic signed [63:0] q, rem, half;
    logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((64'sd1 << sh) - 64'sd1);
      half = 64'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 64'sd1 : 64'sd0);
    end
    if (q > 64'sd32767) rne16q = 16'sh7FFF;
    else if (q < -64'sd32768) rne16q = 16'sh8000;
    else rne16q = q[15:0];
  endfunction

  // nearest-even rescale of a 56-bit accumulator to a saturated 16-bit code
  function automatic logic signed [15:0] rne16w (input logic signed [55:0] p,
                                                 input logic [5:0] sh);
    logic signed [55:0] q, rem, half;
    logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((56'sd1 << sh) - 56'sd1);
      half = 56'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 56'sd1 : 56'sd0);
    end
    if (q > 56'sd32767) rne16w = 16'sh7FFF;
    else if (q < -56'sd32768) rne16w = 16'sh8000;
    else rne16w = q[15:0];
  endfunction

  // nearest-even rescale to a saturated Q4.19 24-bit code
  function automatic logic signed [23:0] rne24w (input logic signed [55:0] p,
                                                 input logic [5:0] sh);
    logic signed [55:0] q, rem, half;
    logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((56'sd1 << sh) - 56'sd1);
      half = 56'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 56'sd1 : 56'sd0);
    end
    if (q > 56'sd8388607) rne24w = 24'sh7FFFFF;
    else if (q < -56'sd8388608) rne24w = 24'sh800000;
    else rne24w = q[23:0];
  endfunction

  function automatic logic signed [31:0] rne32w (input logic signed [55:0] p,
                                                 input logic [5:0] sh);
    logic signed [55:0] q, rem, half;
    logic up;
    if (sh == 0) q = p;
    else begin
      q = p >>> sh;
      rem = p & ((56'sd1 << sh) - 56'sd1);
      half = 56'sd1 << (sh - 6'd1);
      up = (rem > half) | ((rem == half) & q[0]);
      q = q + (up ? 56'sd1 : 56'sd0);
    end
    if (q > 56'sd2147483647) rne32w = 32'sh7FFFFFFF;
    else if (q < -56'sd2147483648) rne32w = 32'sh80000000;
    else rne32w = q[31:0];
  endfunction

  // partial writebacks of the 8-bit low plane (same lane window as mergev)
  function automatic logic [511:0] mergev8 (input logic [511:0] base,
                                            input logic [511:0] src,
                                            input logic [6:0]    off,
                                            input logic [6:0]    wid);
    logic [63:0] lm;
    logic [511:0] m;
    // 8-bit-lane form of the same lane-mask merge (identical window semantics)
    lm = ((64'd1 << wid) - 64'd1) << off;
    for (int i = 0; i < 64; i++) m[i*8 +: 8] = {8{lm[i]}};
    mergev8 = (base & ~m) | (src & m);
  endfunction

  function automatic logic [511:0] shl8 (input logic [511:0] y, input logic [6:0] l);
    shl8 = y << (8*l);
  endfunction

  // ---- matrix / small-parameter descriptor helpers (nano_desc.svh) ----
  // words per column of a standard matrix (K > 64 needs two 64-k blocks)
  function automatic logic [1:0] per_col (input logic [6:0] m);
    per_col = (MAT_K_V[m*9 +: 9] > 9'd64) ? 2'd2 : 2'd1;
  endfunction
  // address of the (kb-th) packed-weight word holding column n: the packer
  // stores element idx = n*K + k at word idx/64, lane idx%64
  function automatic logic [12:0] qz_addr (input logic [6:0] m, input logic [10:0] n,
                                           input logic [1:0] kb);
    logic [19:0] idx;
    idx = 20'(n) * 20'(MAT_K_V[m*9 +: 9]) + 20'(kb) * 20'd64;
    qz_addr = MAT_QZB_V[m*13 +: 13] + 13'(idx >> 6);
  endfunction
  // MAC lane window covering a matrix's K (this model uses K in {32,64,128})
  function automatic logic [2:0] matw (input logic [6:0] m);
    matw = (MAT_K_V[m*9 +: 9] > 9'd32) ? 3'd3 : 3'd1;
  endfunction
  // packed scale slot of column n / k-block kb
  function automatic logic [13:0] sc_slot (input logic [6:0] m, input logic [10:0] n,
                                           input logic [1:0] kb);
    logic [1:0] rlast;
    // packed rows R = ceil(K/min(group_size,K)): with group_size 128 and K 128
    // one row covers both 64-element k-blocks, so both blocks must read it
    // (tools/pack_image.py stores s[min(w,R-1)][n] in every slot).
    rlast = (MAT_K_V[m*9 +: 9] > 9'd64 && cfg_grp64) ? 2'd1 : 2'd0;
    sc_slot = MAT_SCB_V[m*14 +: 14] + 14'(n) * 14'(per_col(m))
            + 14'((kb > rlast) ? rlast : kb);
  endfunction
  // the shared expert / routed expert e (0..7) matrix of a MoE block; kind:
  // 0 = g, 1 = u, 2 = d
  function automatic logic [6:0] moe_shared_mat (input logic [6:0] r, input logic [1:0] kind);
    moe_shared_mat = r + 7'd1 + 7'(kind);
  endfunction
  function automatic logic [6:0] moe_expert_mat (input logic [6:0] r, input logic [2:0] e,
                                                 input logic [1:0] kind);
    moe_expert_mat = r + 7'd4 + 7'(3*e) + 7'(kind);
  endfunction
  function automatic logic [7:0] sm_word_of (input logic [15:0] off);
    sm_word_of = 8'(off >> 4);
  endfunction
  function automatic logic [3:0] sm_slot_of (input logic [15:0] off);
    sm_slot_of = 4'(off & 16'd15);
  endfunction

  // exp(x) for x <= 0 in Q4.11, result in Q1.15 (matches nlin16's exp core)
  function automatic logic [15:0] exp15 (input logic signed [15:0] xin);
    logic signed [47:0] ev;
    ev = exp_core(xin, 5'd15);
    exp15 = (ev > 48'sd32767) ? 16'h7FFF : ev[15:0];
  endfunction
  function automatic logic [15:0] nl_exp15 (input logic signed [15:0] xin);
    nl_exp15 = exp15(xin);
  endfunction

  // Q4.19 split reconstruction: high word is the nearest Q4.11 word, low byte
  // is the signed remainder (value = hi*256 + $signed(lo)).  The VRF stores
  // `canon_hi`/`canon_lo`, so a plain concatenation would zero-extend the byte.
  function automatic logic signed [23:0] j24 (input logic [15:0] hi,
                                               input logic [7:0]  lo);
    j24 = $signed({hi, 8'd0}) + $signed({{16{lo[7]}}, lo});
  endfunction

  // index of the most significant set bit (0 for zero)
  function automatic logic [5:0] msb48 (input logic [47:0] v);
    logic [5:0] r;
    r = 6'd0;
    for (int i = 0; i < 48; i++) if (v[i]) r = i[5:0];
    msb48 = r;
  endfunction
  function automatic logic [5:0] msb56 (input logic [55:0] v);
    logic [5:0] r;
    r = 6'd0;
    for (int i = 0; i < 56; i++) if (v[i]) r = i[5:0];
    msb56 = r;
  endfunction
  // With G = 2^25/sqrt(M) the norm gain only fits 16 bits when M >= 2^20, but
  // the residual stream spans a much wider range.  Drop `s` low bits of G and
  // compensate with a shorter product shift (14 - s), which keeps
  // y = x*gain/sqrt(M) exact and scale independent.
  function automatic logic [4:0] rsqrt_drop (input logic [47:0] v);
    logic [5:0] l;
    l = msb48(v);
    rsqrt_drop = (l < 6'd23) ? 5'(6'd11 - (l >> 1)) : 5'd0;
  endfunction

  // rne(2^24 / X) as a 16-bit word (used for the two-way route weighting)
  function automatic logic [31:0] recip24 (input logic [31:0] X);
    logic [47:0] r;
    r = {16'd0, recip_f({16'd0, X}, 6'd24)};
    recip24 = r[31:0];
  endfunction

  logic [8:0] mK; logic [9:0] mN; logic [1:0] mper; logic [7:0] mR;
  logic [13:0] mscb; logic [12:0] mqzb, mwc;
  assign mK   = MAT_K_V[ld_mat*9 +: 9];
  assign mN   = MAT_N_V[ld_mat*10 +: 10];
  assign mper = (mK == 9'd128) ? 2'd2 : 2'd1;
  assign mR   = (mK == 9'd128 && cfg_grp64) ? 8'd2 : 8'd1;
  assign mscb = MAT_SCB_V[ld_mat*14 +: 14];
  assign mqzb = MAT_QZB_V[ld_mat*13 +: 13];
  assign mwc  = MAT_WC_V[ld_mat*13 +: 13];
  wire [31:0] m_bytes = 32'(3 * mR * mN) + 32'(mK * mN / 2);

  logic [7:0] ld_byte;
  assign ld_byte = ld_beat[ld_bidx*8 +: 8];

  // per-token program parameters (set by the program steps)
  logic [3:0] p_src, p_dst, p_dst2;
  logic [6:0] p_mat;
  logic [9:0] p_n; logic [8:0] p_len;
  logic [15:0] p_smoff; logic [1:0] p_smkind; logic [3:0] p_smwords;
  logic [2:0] p_op; logic [4:0] p_sh; logic signed [15:0] p_sc;
  logic [1:0] p_redop, p_sfuop, p_nlop;
  logic [5:0] p_sfush; logic [4:0] p_nlosh;
  logic [1:0] p_head; logic [4:0] p_log2n; logic [23:0] p_eps;
  logic [1:0] p_expert; logic [3:0] p_stream;
  logic [15:0] ret_step;
  logic [3:0]  p_slo, p_shi, p_tmp;
  logic [3:0]  p_dhi;                 // high-half destination of a 128-wide GEMV
                                      // (the VRF pair is S_B/S_C, not always
                                      // p_dst+1, so the slot is explicit)
  logic [7:0]  p_base;
  logic        p_wide;                // ALU operates on the 24-bit representation
  logic        p_b24;                 // wide op2 multiplies two 24-bit operands
  logic        p_gwide;               // GEMV writes a Q4.19 24-bit result

  // The write port reads the slot it overwrites so partial writebacks merge
  // (VRF read port C is dedicated to that merge base).
  always_comb vrf_rc = vrf_wa;

  // ---- helpers used by the shared executors (nano_exec.svh) ----
  // one 256-bit small-parameter word as 16 low lanes of a 64-lane vector
  function automatic logic [1023:0] smw (input logic [255:0] w);
    logic [1023:0] r;
    for (int i = 0; i < 64; i++) r[i*16 +: 16] = (i < 16) ? w[i*16 +: 16] : 16'd0;
    smw = r;
  endfunction
  // MLA key row of the current position: [h0: kc(32) kr(16)][h1: kc(32) kr(16)]
  function automatic logic [1535:0] asm_key (input logic [1023:0] kc, input logic [1023:0] kr);
    logic [1535:0] r;
    for (int j = 0; j < 96; j++) begin
      // the (j-16) kc index is masked into range so that every branch of the
      // chain is a legal bit select for all j (synthesis evaluates all arms)
      r[j*16 +: 16] = (j < 32) ? kc[j*16 +: 16]
                    : (j < 48) ? kr[(j-32)*16 +: 16]
                    : (j < 80) ? kc[(((j-16) & 63))*16 +: 16]
                    :            kr[((j-80) & 63)*16 +: 16];
    end
    asm_key = r;
  endfunction
  // MLA query vector of `head` (48 lanes) from qc (2x32) and qr (2x16)
  function automatic logic [1023:0] asm_q (input logic [1023:0] qc, input logic [1023:0] qr,
                                           input logic head);
    logic [1023:0] r;
    logic [6:0] qcbase, qrbase;
    qcbase = head ? 7'd32 : 7'd0;
    qrbase = head ? 7'd16 : 7'd0;
    for (int j = 0; j < 64; j++) begin
      if (j < 32)      r[j*16 +: 16] = qc[(qcbase + 7'(j))*16 +: 16];
      else if (j < 48) r[j*16 +: 16] = qr[(qrbase + 7'(j - 32))*16 +: 16];
      else             r[j*16 +: 16] = 16'd0;
    end
    asm_q = r;
  endfunction
  // index of the largest of 8 lanes, smallest index on ties; `excl` optionally
  // removes one lane (the second MoE selection)
  function automatic logic [2:0] argmax8 (input logic [1023:0] v, input logic [2:0] excl,
                                          input logic have_excl);
    logic signed [15:0] bestv;
    logic [2:0] besti;
    bestv = 16'sh8000;
    besti = 3'd0;
    for (int i = 0; i < 8; i++) begin
      if (!(have_excl && (i[2:0] == excl)) && ($signed(v[i*16 +: 16]) > bestv)) begin
        bestv = $signed(v[i*16 +: 16]);
        besti = i[2:0];
      end
    end
    argmax8 = besti;
  endfunction

  // 24-bit (Q4.19) variant used by the MoE router comparison
  function automatic logic [2:0] argmax8w (input logic [1023:0] v,
                                           input logic [511:0]  vl,
                                           input logic [2:0]    excl,
                                           input logic          have_excl);
    logic signed [23:0] bestv, cur;
    logic [2:0] besti;
    bestv = 24'sh800000;
    besti = 3'd0;
    for (int i = 0; i < 8; i++) begin
      cur = j24(v[i*16 +: 16], vl[i*8 +: 8]);
      if (!(have_excl && (i[2:0] == excl)) && (cur > bestv)) begin
        bestv = cur;
        besti = i[2:0];
      end
    end
    argmax8w = besti;
  endfunction
  // move lane 0 of a vector to lane `l` before a mergev writeback
  function automatic logic [1023:0] shl16 (input logic [1023:0] y, input logic [6:0] l);
    shl16 = y << (16*l);
  endfunction
  // rotating KDA convolution history row for tap t of a stream
  function automatic logic [1:0] hrow (input logic [1:0] ptr, input logic [1:0] t);
    logic [2:0] s;
    s = 3'(ptr) + 3'(t);
    hrow = (s >= 3'd3) ? 2'(s - 3'd3) : 2'(s);
  endfunction

  // MAC-accumulator writeback: lanes [ow_off, ow_off+wsel(ow_w)) come from the
  // rescaled MAC result, all other lanes keep their current value.
  function automatic logic [1023:0] macwb (input logic [1023:0] base);
    macwb = mergev(base, mac_acc_out, ow_off, wsel(ow_w));
  endfunction

  // =========================================================================
  // Posted-write port.  It is driven combinationally from the drain beat so
  // that the payload present in a cycle is exactly the beat nano_mem accepts
  // in that cycle.  (A registered payload would lag the beat counter by one
  // beat: word 0 would be duplicated, every later word would be shifted by one
  // and the argmax beat would be issued after the phase left P_DRAIN.)
  // Outside P_DRAIN no write request is issued.
  // =========================================================================
  always_comb begin
    wr_issue = (phase == P_DRAIN);
    wr_addr  = cfg_obase + 36'(36'(tok) * 36'd2064) + 36'(36'(drain_beat) * 36'd16);
    if (drain_beat < 16'd128) begin
      wr_data = lg_rd;
      wr_strb = 16'hFFFF;
    end else begin
      wr_data = {96'd0, 16'd0, argi};
      wr_strb = 16'h000F;
    end
  end

  // =========================================================================
  // Presented macro read addresses.  Each executor sets a read address in the
  // cycle `t` in which it needs the address on the port; the synchronous macro
  // returns the data in cycle t+1, which is where the executor consumes it.
  // =========================================================================
  always_comb begin
    qz_ra_p = qz_ra;
    sc_ra_p = sc_ra;
    sm_ra_p = sm_ra;
    cv_ra_p = cv_ra;
    ck_ra_p = ck_ra;
    kk_ra_p = kk_ra;
    vv_ra_p = vv_ra;
    st_ra_p = st_ra;

    if (phase == P_STEP) begin
      case (step)
        E0_EMB: begin
          qz_ra_p = 12'(MAT_QZB_V[M_EMB*13 +: 13] + 13'(tok_buf[tok[5:0]]));
          sc_ra_p = 9'((MAT_SCB_V[M_EMB*14 +: 14] + 14'(tok_buf[tok[5:0]])) / 14'd10);
        end
        X_GEMV, X_GEMV2: begin
          if (sub == 6'd0) begin
            qz_ra_p = qz_addr(p_mat, 10'd0, 2'd0);
            sc_ra_p = 9'(sc_slot(p_mat, 10'd0, 2'd0) / 14'd10);
          end else if (sub == 6'd1) begin
            qz_ra_p = qz_addr(p_mat, gv_col, gv_kb);
            sc_ra_p = 9'(sc_slot(p_mat, gv_col, gv_kb) / 14'd10);
          end
        end
        X_SML: begin
          if (sub[0] == 1'b0) sm_ra_p = sm_word_of(p_smoff) + 8'(sub >> 1);
        end
        X_NORM, X_NORMH: begin
          if (sub < 6'd16 && sub[0] == 1'b0) sm_ra_p = sm_word_of(p_smoff) + 8'(sub >> 1);
        end
        X_ALPHA: begin
          if (sub[0] == 1'b0 && sub < 6'd8)
            sm_ra_p = sm_word_of(16'(SM_OFF_V[S_L0_KDA_DTB*16 +: 16])) + 8'(sub >> 1);
          if (sub == 6'd7) sm_ra_p = sm_word_of(16'(SM_OFF_V[S_L0_KDA_A*16 +: 16]));
        end
        X_CONV: begin
          if (sub < 6'd8) begin
            if (sub < 6'd6) begin
              cv_ra_p = 4'(3*p_stream + 4'(hrow(hist_ptr[p_stream], 2'(sub[2:1]))));
              ck_ra_p = 4'(4*p_stream + 4'(sub >> 1));
            end else begin
              ck_ra_p = 4'(4*p_stream + 4'd3);
            end
          end
        end
        X_ATTN: begin
          if (sub == 6'd5 || sub == 6'd6)  kk_ra_p = at_pos[5:0];
          if (sub == 6'd20 || sub == 6'd21) vv_ra_p = at_pos[5:0];
        end
        X_STATE: begin
          if (sub == 6'd1 || sub == 6'd2 || sub == 6'd3 ||
              sub == 6'd5 || sub == 6'd6 || sub == 6'd7 ||
              sub == 6'd13 || sub == 6'd14 || sub == 6'd15 || sub == 6'd16 ||
              sub == 6'd18 || sub == 6'd19 || sub == 6'd20 || sub == 6'd21 ||
              sub == 6'd54 || sub == 6'd55 || sub == 6'd56)
            st_ra_p = 7'(32*p_head + st_row[6:0]);
        end
        default: ;
      endcase
    end
  end

  assign cyc_count = cycles;
  assign core_busy = busy;
  assign cmd_ready = (phase == P_IDLE) && !rsp_hold;
  assign rsp_valid = rsp_hold;
  assign rsp_data  = DONE_W;

  // =========================================================================
  // control + state: one registered process (see nano_prog.svh for the token
  // program body, which is textually included here)
  // =========================================================================
  // Spec §5: rst_n is an active-low synchronous reset sampled on the rising
  // edge.  A synchronous (not asynchronous) reset also keeps the shared SRAM
  // macros, which have no reset, independent of the reset edge; every RUN
  // re-initialises them explicitly in P_INIT instead.
`ifdef NANO_PROFILE
  // Development-only cycle histogram (not part of the deliverable netlist).
  integer prof_step [0:255];
  integer prof_sub0 [0:255];
  integer prof_phase [0:15];
  integer prof_loadst [0:7];
  initial begin
    for (int i = 0; i < 256; i++) begin prof_step[i] = 0; prof_sub0[i] = 0; end
    for (int i = 0; i < 16; i++) prof_phase[i] = 0;
    for (int i = 0; i < 8; i++) prof_loadst[i] = 0;
  end
  always_ff @(posedge clk) begin
    prof_phase[phase] <= prof_phase[phase] + 1;
    if (phase == P_STEP) begin
      prof_step[step] <= prof_step[step] + 1;
      if (sub == 6'd0) prof_sub0[step] <= prof_sub0[step] + 1;
    end
    if (phase == P_LOAD) prof_loadst[ld_st] <= prof_loadst[ld_st] + 1;
  end
  always_ff @(posedge clk) if (phase == P_DONE) begin
    for (int i = 0; i < 256; i++)
      if (prof_step[i] != 0) $display("PROF step %0d count %0d sub0 %0d", i, prof_step[i], prof_sub0[i]);
    for (int i = 0; i < 16; i++)
      if (prof_phase[i] != 0) $display("PROF phase %0d count %0d", i, prof_phase[i]);
    for (int i = 0; i < 8; i++)
      if (prof_loadst[i] != 0) $display("PROF loadst %0d count %0d", i, prof_loadst[i]);
  end
`endif

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      phase <= P_IDLE; cfg_seq_m8 <= '0; cfg_grp64 <= 1'b0;
      cfg_wbase <= '0; cfg_tbase <= '0; cfg_obase <= '0;
      tok <= '0; step <= '0; sub <= '0; cycles <= '0; busy <= 1'b0;
      drain_beat <= '0; rsp_hold <= 1'b0;
      ld_mat <= '0; ld_total <= '0; ld_st <= '0; ld_smid <= '0;
      ld_beat_valid <= 1'b0; ld_bidx <= '0; ld_req <= '0; ld_nb <= '0; ld_out <= '0;
      init_cnt <= '0; gv_busy <= 1'b0; gv_word <= '0; gv_ph <= '0; gv_wide <= 1'b0;
      argi <= '0; argv <= 32'sh80000000; cvrow <= '0; cpos <= '0;
      gv_col <= '0; gv_kb <= '0; gv_acc <= '0; gv_dot <= '0; gv_sc <= '0;
      gv_code <= '0; gv_max <= 32'sh80000000; gv_argi <= '0;
      nm_blk <= '0; nm_ss <= '0; nm_m <= '0; nm_g <= '0;
      al_a0 <= '0; al_a1 <= '0; st_row <= '0; st_beta <= '0;
      at_pos <= '0; at_dot <= '0; at_tot <= '0;
      cn_ph <= '0; mz_sel0 <= '0; mz_sel1 <= '0; mz_s0 <= '0; mz_s1 <= '0;
      ex_w <= '0; ex_p <= '0; sd_ph <= 1'b0; red_win <= 2'd0;
      at_mx <= '0; at_r <= '0; sfu_r <= '0; mz_w0 <= '0; mz_w1 <= '0;
      nm_i <= '0;
      nm_drop <= '0;
      for (int i = 0; i < 3; i++) hist_ptr[i] <= 2'd0;
      oa_src <= 3'd0; alu_a_src <= 3'd0; alu_b_src <= 3'd0;
      ck_wstrb <= '0; cv_wstrb <= '0; kk_wstrb <= '0; vv_wstrb <= '0;
      vrf_we <= 1'b0; vrf_ra <= S_A; vrf_rb <= S_B; vrf_wa <= S_A;
      mac_en <= 1'b0; mac_clr <= 1'b0; mac_wmode <= 1'b0;
      mac_scl <= 24'd1; mac_sh <= 6'd11;
      oa_off <= 2'd0; oa_w <= 3'd3; ob_off <= 2'd0; ob_w <= 3'd3;
      ob_src <= 2'd0; ob_blane <= 6'd0; ow_off <= 2'd0; ow_w <= 3'd3;
      alu_op <= 3'd6; alu_sh <= 5'd0; alu_s <= 16'sd0; alu_w <= 3'd3;
      alu_soff <= 7'd0; alu_s_vd <= 1'b0; alu_wide <= 1'b0;
      p_wide <= 1'b0; p_b24 <= 1'b0; p_gwide <= 1'b0; vrf_lwd <= '0;
      alu_b24 <= 1'b0;
      alu_aoff <= 6'd0; alu_boff <= 6'd0;
      nl_op <= 2'd0; nl_osh <= 5'd11; nl_off <= 6'd0;
      sfu_op <= 2'd0; sfu_din <= 48'd0; sfu_din2 <= 48'd0; sfu_sh <= 6'd0;
      red_op <= 2'd0;
      qz_ra <= 12'd0; qz_we <= 1'b0; qz_wa <= 12'd0; qz_wd <= 320'd0;
      sc_ra <= 9'd0; sc_we <= 1'b0; sc_wa <= 9'd0; sc_wd <= 160'd0; sc_wstrb <= 20'd0;
      sm_ra <= 8'd0; sm_we <= 1'b0; sm_wa <= 8'd0; sm_wd <= 256'd0; sm_wstrb <= 32'd0;
      st_ra <= 6'd0; st_we <= 1'b0; st_wa <= 6'd0; st_wd <= 768'd0;
      cv_ra <= 4'd0; cv_we <= 1'b0; cv_wa <= 4'd0; cv_wd <= 1024'd0;
      ck_ra <= 4'd0; ck_we <= 1'b0; ck_wa <= 4'd0; ck_wd <= 1024'd0;
      kk_ra <= 6'd0; kk_we <= 1'b0; kk_wa <= 6'd0; kk_wd <= 1536'd0;
      vv_ra <= 6'd0; vv_we <= 1'b0; vv_wa <= 6'd0; vv_wd <= 1024'd0;
      lg_ra <= 7'd0; lg_we <= 1'b0; lg_wa <= 7'd0; lg_wd <= 128'd0; lg_wstrb <= 16'd0;
      for (int i = 0; i < 4; i++) cv_base[i] <= '0;
    end else begin
      // ---- defaults each cycle ----
      vrf_we <= 1'b0;
      vrf_lwd <= 512'd0;
      mac_en <= 1'b0; mac_clr <= 1'b0;
      p_wide <= 1'b0; p_b24 <= 1'b0; p_gwide <= 1'b0;
      qz_we <= 1'b0; sc_we <= 1'b0; sm_we <= 1'b0; st_we <= 1'b0;
      cv_we <= 1'b0; ck_we <= 1'b0; kk_we <= 1'b0; vv_we <= 1'b0; lg_we <= 1'b0;
      if (busy) cycles <= cycles + 32'd1;
      if (latch_red) red_lat <= red_y;
      if (latch_dot) begin dot_lat <= mac_dot; dot2_lat <= mac_dot_lo; end
      // weight-stream byte accounting (see the window logic above)
      if (phase == P_LOAD) ld_bytecnt <= ld_bytecnt + 32'(ld_c);
      `include "nano_prog.svh"
    end
  end

  // latch strobes are driven by the program fragment
  logic latch_red, latch_dot;
endmodule
