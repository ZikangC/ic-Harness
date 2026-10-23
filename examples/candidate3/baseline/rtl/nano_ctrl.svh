// ---------------------------------------------------------------------------
// Sequencer control decode for nano_core: combinational control of the VRF,
// compute units, storage macros and memory interface from the phase/step
// state, plus the token program parameter registers.  This file is included by
// nano_core.sv.
// ---------------------------------------------------------------------------

  // ---- step ids (token program) ----
  localparam [15:0]
    E_EMB = 16'd1,
    E_CPX0 = 16'd2,
    E_CALL_NORM = 16'd3,
    E_CALL_GEMV = 16'd4,
    E_CALL_CONV = 16'd5,
    E_CALL_EXPERT = 16'd6,
    E_CALL_MOE = 16'd7,
    E_CALL_RMIX = 16'd8,
    E_SMLOAD = 16'd9,
    E_SMLOAD_W = 16'd10,
    E_ALUS = 16'd11,
    E_ALUV = 16'd12,
    E_RED = 16'd13,
    E_SFU = 16'd14,
    E_NL = 16'd15,
    E_MACDOT = 16'd16,
    E_MACRES = 16'd17,
    E_MACACC = 16'd18,
    E_STATE = 16'd19,      // KDA state walk for one head
    E_ALPHA2 = 16'd20,     // f+dtb, *A, sigmoid, *-5, exp
    E_ATTN = 16'd21,       // MLA scoring over positions
    E_SOFTMAX = 16'd22,    // attention softmax + context
    E_L2 = 16'd23,         // per-head L2 normalization of a 64-wide vector
    E_SIGZ = 16'd24,       // sigmoid(z) then multiply
    E_TOP2 = 16'd25,       // router top-2 selection + route weights
    E_SETX = 16'd26,       // x = a + b
    E_MOEACC = 16'd27,     // weighted expert accumulation
    E_KVAPP = 16'd28,      // append the MLA key/value rows
    E_DRAIN = 16'd29,
    E_FIN = 16'd30;

  // ---- program step registers ----
  logic [15:0] ret_step;
  logic [3:0]  p_src, p_dst, p_dst2, p_tmp;
  logic [6:0]  p_mat, p_mat2;
  logic [9:0]  p_n;
  logic [8:0]  p_len;
  logic [7:0]  p_smoff;
  logic [1:0]  p_smkind;
  logic [3:0]  p_smwords;
  logic [2:0]  p_op;
  logic [4:0]  p_sh;
  logic signed [15:0] p_sc;
  logic signed [15:0] p_sc2;
  logic [1:0]  p_redop;
  logic [1:0]  p_sfuop;
  logic [5:0]  p_sfush;
  logic [1:0]  p_nlop;
  logic [4:0]  p_nlosh;
  logic [1:0]  p_head;
  logic [4:0]  p_log2n;
  logic [5:0]  p_eps;
  logic [1:0]  p_expert;      // 0 shared, 1 sel0, 2 sel1
  logic [1:0]  p_mode;        // gemv K mode
  logic [3:0]  p_stream;      // conv stream / L2 target slot
  logic [7:0]  p_base;        // layer base matrix index (7 bits used)

  // -------------------------------------------------------------------------
  // control decode (combinational)
  // -------------------------------------------------------------------------
  logic [63:0] wmask;
  logic [1023:0] wr_res;
  logic [6:0]  wlane_w;
  logic [3:0]  nw;

  // small-param read word (combinational address)
  logic [7:0]  sm_word;

  always_comb begin
    // defaults
    vrf_ra = p_src; vrf_rb = p_dst; vrf_rc = 4'd0;
    vrf_wa = p_dst; vrf_we = 1'b0; vrf_wd = 1024'd0;
    mac_clr = 1'b0; mac_en = 1'b0;
    mac_scl = 24'd1; mac_sh = 6'd11;
    alu_op = 3'd6; alu_sh = 5'd0; alu_s = 16'sd0; alu_w = 3'd3;
    alu_aoff = 6'd0; alu_boff = 6'd0; alu_woff = 6'd0;
    nl_op = 2'd0; nl_osh = 5'd11; nl_off = 6'd0;
    sfu_op = 2'd0; sfu_din = 48'd0; sfu_din2 = 48'd0; sfu_sh = 6'd0;
    red_op = 2'd0;
    oa_off = 2'd0; oa_w = 3'd3; ob_off = 2'd0; ob_w = 3'd3;
    ob_src = 2'd0; ob_blane = 6'd0;
    ow_off = 2'd0; ow_w = 3'd3;
    sm_word = p_smoff;
    sm_ra = (sm_word + {4'b0, sub[3:0]}) > 8'd255 ? 8'd0 : (sm_word + {4'b0, sub[3:0]});
    rd_issue = 1'b0; rd_addr = 36'd0;
    fifo_pop = 1'b0;
    wr_issue = 1'b0; wr_addr = 36'd0; wr_data = 128'd0; wr_strb = 16'd0;
    qz_ra = 12'd0; qz_we = 1'b0; qz_wa = 12'd0; qz_wd = 320'd0;
    sc_ra = 9'd0; sc_we = 1'b0; sc_wa = 9'd0; sc_wd = 160'd0; sc_wstrb = 20'd0;
    sm_we = 1'b0; sm_wa = 8'd0; sm_wd = 256'd0; sm_wstrb = 32'd0;
    st_ra = 6'd0; st_we = 1'b0; st_wa = 6'd0; st_wd = 512'd0;
    cv_ra = 4'd0; cv_we = 1'b0; cv_wa = 4'd0; cv_wd = 1024'd0;
    kk_ra = 6'd0; kk_we = 1'b0; kk_wa = 6'd0; kk_wd = 1536'd0;
    vv_ra = 6'd0; vv_we = 1'b0; vv_wa = 6'd0; vv_wd = 1024'd0;
    lg_ra = 7'd0; lg_we = 1'b0; lg_wa = 7'd0; lg_wd = 128'd0; lg_wstrb = 16'd0;

    // VRF operand selection for the shared MAC during "direct" steps
    case (p_op)
      3'd0: begin oa_off = 2'd0; oa_w = 3'd3; end       // full 64
      default: ;
    endcase

    if (phase == P_LOAD) begin
      // loader drives the write ports (details in the sequential block)
    end
  end

  // -------------------------------------------------------------------------
  // loader byte plumbing (combinational)
  // -------------------------------------------------------------------------
  logic [7:0]  ld_byte;
  wire         ld_want_beat = !ld_beat_valid;
  assign ld_byte = ld_beat[ld_bidx*8 +: 8];
