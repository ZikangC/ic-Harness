// ---------------------------------------------------------------------------
// Per-token program for nano_core (included inside the P_STEP branch).
//
// The graph follows reference.py step() exactly:
//   embedding -> [L0 KDA -> res_mix -> MoE] -> [L1 MLA -> res_mix -> MoE]
//   -> res_out res_mix -> final RMSNorm -> LM head (logits + argmax).
//
// Control style: every graph step sets the parameter registers (p_*) and jumps
// to a shared executor (nano_exec.svh), which returns to ret_step.  The numeric
// conventions are PLAN.md section 2; the operand conventions are documented at
// each executor.
//
// VRF slot map (PLAN.md section 4):
//   A normed GEMV input / general      B GEMV output / general
//   C GEMV output high half / MLA context / MoE scratch
//   D general (KDA alpha, MLA kc, res_mix projection / expert u)
//   X residual stream      X0 snapshot      P prefix (attention mix)
//   T mixer output (KDA/MLA out)          Q qc      K qr
//   V KDA value / MLA v / gated KDA out   F MLA kr / KDA f
//   Z KDA z / gate                        H KDA head out / MoE expert out
//   M MoE out                             G small-param scratch / MoE router
// ---------------------------------------------------------------------------
  case (step)
    // ---------------- embedding: x = (q - z) * s * 2^-12 ----------------
    E0_EMB: begin
      if (sub == 6'd0) begin
        // embedding lookup: the packed column is the token id (not the position)
        qz_ra <= 12'(MAT_QZB_V[M_EMB*13 +: 13] + 13'(tok_buf[tok[5:0]]));
        sc_ra <= 9'((MAT_SCB_V[M_EMB*14 +: 14] + 14'(tok_buf[tok[5:0]])) / 14'd10);
        sub <= 6'd1;
      end else if (sub == 6'd1) begin
        for (int k = 0; k < 64; k++) begin
          logic signed [15:0] qz5;
          logic signed [15:0] scl;
          logic signed [63:0] c24;
          qz5 = $signed(qz_rd[k*5 +: 5]);
          scl = $signed(sc_rd[16*((MAT_SCB_V[M_EMB*14 +: 14] + 14'(tok_buf[tok[5:0]])) % 14'd10) +: 16]);
          // value = (q-z)*s*2^-12; Q4.19 code = (q-z)*s*2^7 (exact)
          c24 = 64'($signed(qz5)) * 64'($signed(scl)) * 64'sd128;
          vrf_wd[k*16 +: 16] <= canon_hi(c24);
          vrf_lwd[k*8 +: 8] <= canon_lo(c24);
        end
        vrf_wa <= S_X; vrf_we <= 1'b1;
        sub <= 6'd2;
      end else begin
        sub <= 6'd0; step <= E0_X0;
      end
    end

    E0_X0: begin
      if (sub == 6'd0) begin
        vrf_ra <= S_X; vrf_wa <= S_X0; vrf_we <= 1'b0; sub <= 6'd1;
      end else begin
        vrf_wd <= vrf_va; vrf_lwd <= vrf_lva; vrf_we <= 1'b1;
        sub <= 6'd0; step <= E0_N1L0;
      end
    end

    // ---------------- layer 0: KDA ----------------
    E0_N1L0: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L0_NORM1_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E0_KQ; step <= X_NORM; sub <= 6'd0; end
    E0_KQ:  begin p_mat <= 7'(M_L0_KDA_Q); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd64; ret_step <= E0_KQ2; step <= X_GEMV; sub <= 6'd0; end
    E0_KQ2: begin p_src <= S_B; p_dst <= S_B; p_stream <= 4'd0;
                  ret_step <= E0_KK; step <= X_CONV; sub <= 6'd0; end
    E0_KK:  begin p_mat <= 7'(M_L0_KDA_K); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd64; ret_step <= E0_KK2; step <= X_GEMV; sub <= 6'd0; end
    E0_KK2: begin p_src <= S_B; p_dst <= S_B; p_stream <= 4'd1;
                  ret_step <= E0_KV; step <= X_CONV; sub <= 6'd0; end
    E0_KV:  begin p_mat <= 7'(M_L0_KDA_V); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd64; ret_step <= E0_KV2; step <= X_GEMV; sub <= 6'd0; end
    E0_KV2: begin p_src <= S_B; p_dst <= S_B; p_stream <= 4'd2;
                  ret_step <= E0_KB; step <= X_CONV; sub <= 6'd0; end
    E0_KB:  begin p_mat <= 7'(M_L0_KDA_B); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd2; ret_step <= E0_KB2; step <= X_GEMV; sub <= 6'd0; end
    E0_KB2: begin
      if (sub == 6'd0) begin vrf_ra <= S_B; nl_op <= 2'd1; nl_off <= 6'd0; sub <= 6'd1; end
      else begin beta0 <= $signed(nl_y[15:0]); beta1 <= $signed(nl_y[31:16]);
                 sub <= 6'd0; step <= E0_KF; end
    end
    E0_KF:  begin p_mat <= 7'(M_L0_KDA_F1); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd32; ret_step <= E0_KF2; step <= X_GEMV; sub <= 6'd0; end
    E0_KF2: begin p_mat <= 7'(M_L0_KDA_F2); p_slo <= S_B; p_shi <= S_B; p_dst <= S_F;
                  p_n <= 10'd64; ret_step <= E0_KG; step <= X_GEMV; sub <= 6'd0; end
    E0_KG:  begin p_mat <= 7'(M_L0_KDA_G1); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B;
                  p_n <= 10'd32; ret_step <= E0_KG2; step <= X_GEMV; sub <= 6'd0; end
    E0_KG2: begin p_mat <= 7'(M_L0_KDA_G2); p_slo <= S_B; p_shi <= S_B; p_dst <= S_Z;
                  p_n <= 10'd64; ret_step <= E0_ALPHA; step <= X_GEMV; sub <= 6'd0; end
    E0_ALPHA: begin ret_step <= E0_L2QK; step <= X_ALPHA; sub <= 6'd0; end
    E0_L2QK: begin p_src <= S_Q; ret_step <= E0_L2K; step <= X_L2; sub <= 6'd0; end
    E0_L2K:  begin p_src <= S_K; ret_step <= E0_ST0; step <= X_L2; sub <= 6'd0; end
    E0_ST0: begin p_head <= 2'd0; ret_step <= E0_ST1; step <= X_STATE; sub <= 6'd0; end
    E0_ST1: begin p_head <= 2'd1; ret_step <= E0_KOUT; step <= X_STATE; sub <= 6'd0; end
    E0_KOUT: begin p_src <= S_H; p_dst <= S_V; p_smoff <= 16'(SM_OFF_V[S_L0_KDA_ONORM_W*16 +: 16]);
                   // head readout is a Q1.18 code: mean square is in 2^36 units, eps 1e-5*2^36
                   p_len <= 9'd64; p_log2n <= 5'd5; p_eps <= 24'd687195;
                   ret_step <= E0_SIGZ; step <= X_NORMH; sub <= 6'd0; end
    E0_SIGZ: begin p_src <= S_V; p_dst <= S_Z; ret_step <= E0_KO; step <= X_SIGMUL; sub <= 6'd0; end
    E0_KO:  begin p_mat <= 7'(M_L0_KDA_O); p_slo <= S_V; p_shi <= S_V; p_dst <= S_T;
                  p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E0_RM0A; step <= X_GEMV; sub <= 6'd0; end

    // ---------------- layer 0: res_mix + MoE ----------------
    E0_RM0A: begin p_src <= S_X0; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L0_RES_MLP_NORM_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E0_RM0B; step <= X_NORM; sub <= 6'd0; end
    E0_RM0B: begin p_src <= S_A; p_dst <= S_D; p_smkind <= 2'd1; p_smwords <= 4'd4;
                   p_smoff <= 16'(SM_OFF_V[S_L0_RES_MLP_PROJ*16 +: 16]);
                   ret_step <= E0_RM0B2; step <= X_SML; sub <= 6'd0; end
    E0_RM0B2: begin p_src <= S_D; p_dst <= S_A; ret_step <= E0_RM0B3; step <= X_DOT; sub <= 6'd0; end
    E0_RM0B3: begin logit0 <= dot_lat; sub <= 6'd0; step <= E0_RM0C; end
    E0_RM0C: begin p_src <= S_T; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L0_RES_MLP_NORM_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E0_RM0D; step <= X_NORM; sub <= 6'd0; end
    E0_RM0D: begin p_src <= S_D; p_dst <= S_A; ret_step <= E0_RM0D2; step <= X_DOT; sub <= 6'd0; end
    E0_RM0D2: begin logit1 <= dot_lat; sub <= 6'd0; step <= E0_RM0E; end
    E0_RM0E: begin p_dst <= S_X0; p_dst2 <= S_T; p_tmp <= S_X; ret_step <= E0_N2L0;
                   step <= X_RMIX; sub <= 6'd0; end
    E0_N2L0: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L0_NORM2_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E0_MOES; step <= X_NORM; sub <= 6'd0; end
    // MoE: router -> sigmoid -> bias -> stable top-2 -> shared + 2 routed experts
    E0_MOES: begin p_mat <= 7'(M_L0_MOE_ROUTER); p_slo <= S_A; p_shi <= S_A; p_dst <= S_M;
                   p_n <= 10'd8; p_gwide <= 1'b1;
                   ret_step <= E0_MOSG; step <= X_GEMV; sub <= 6'd0; end
    E0_MOSG: begin p_src <= S_M; p_dst <= S_M; ret_step <= E0_MOBI; step <= X_SIG24; sub <= 6'd0; end
    E0_MOS2: begin ret_step <= E0_MOBI; step <= X_SIG24; sub <= 6'd0; end
    E0_MOBI: begin p_dst <= S_B; p_smkind <= 2'd3; p_smwords <= 4'd1;
                   p_smoff <= 16'(SM_OFF_V[S_L0_MOE_BIAS*16 +: 16]);
                   ret_step <= E0_MOB2; step <= X_SML; sub <= 6'd0; end
    E0_MOB2: begin p_op <= 3'd0; p_sh <= 5'd0; p_sc <= 16'sd0; p_src <= S_M; p_dst <= S_B;
                   p_dst2 <= S_G; p_len <= 9'd16; p_wide <= 1'b1;
                   ret_step <= E0_MOTT; step <= X_ALU; sub <= 6'd0; end
    E0_MOTT: begin ret_step <= E0_MG0; step <= X_TOP2; sub <= 6'd0; end
    E0_MG0: begin p_mat <= 7'(moe_shared_mat(M_L0_MOE_ROUTER, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MU0; step <= X_GEMV; sub <= 6'd0; end
    E0_MU0: begin p_mat <= 7'(moe_shared_mat(M_L0_MOE_ROUTER, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MA0; step <= X_GEMV; sub <= 6'd0; end
    E0_MA0: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E0_MM0; step <= X_SILU24; sub <= 6'd0; end
    E0_MM0: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E0_MD0; step <= X_ALU; sub <= 6'd0; end
    E0_MD0: begin p_mat <= 7'(moe_shared_mat(M_L0_MOE_ROUTER, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E0_MZ0; step <= X_GEMV; sub <= 6'd0; end
    E0_MZ0: begin p_src <= S_H; p_sc <= 16'sd8192; ret_step <= E0_MG1; step <= X_MOEACC; sub <= 6'd0; end
    E0_MG1: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel0, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MU1; step <= X_GEMV; sub <= 6'd0; end
    E0_MU1: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel0, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MA1; step <= X_GEMV; sub <= 6'd0; end
    E0_MA1: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E0_MM1; step <= X_SILU24; sub <= 6'd0; end
    E0_MM1: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E0_MD1; step <= X_ALU; sub <= 6'd0; end
    E0_MD1: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel0, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E0_MZ1; step <= X_GEMV; sub <= 6'd0; end
    E0_MZ1: begin p_src <= S_H; p_sc <= mz_w0; ret_step <= E0_MG2; step <= X_MOEACC; sub <= 6'd0; end
    E0_MG2: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel1, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MU2; step <= X_GEMV; sub <= 6'd0; end
    E0_MU2: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel1, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E0_MA2; step <= X_GEMV; sub <= 6'd0; end
    E0_MA2: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E0_MM2; step <= X_SILU24; sub <= 6'd0; end
    E0_MM2: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E0_MD2; step <= X_ALU; sub <= 6'd0; end
    E0_MD2: begin p_mat <= 7'(moe_expert_mat(M_L0_MOE_ROUTER, mz_sel1, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E0_MZ2; step <= X_GEMV; sub <= 6'd0; end
    E0_MZ2: begin p_src <= S_H; p_sc <= mz_w1; ret_step <= E0_MF; step <= X_MOEACC; sub <= 6'd0; end
    E0_MF:  begin p_src <= S_T; p_dst <= S_M; p_dst2 <= S_X; ret_step <= E1_PRE;
                  step <= X_ADDV; sub <= 6'd0; end

    // ---------------- layer 1: NoPE MLA ----------------
    E1_PRE: begin
      if (sub == 6'd0) begin vrf_ra <= S_X; sub <= 6'd1; end
      else begin vrf_wd <= vrf_va; vrf_lwd <= vrf_lva; vrf_wa <= S_P; vrf_we <= 1'b1;
                 sub <= 6'd0; step <= E1_RM1A; end
    end
    E1_RM1A: begin p_src <= S_X0; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_RES_ATTN_NORM_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E1_RM1A2; step <= X_NORM; sub <= 6'd0; end
    E1_RM1A2: begin p_src <= S_A; p_dst <= S_D; p_smkind <= 2'd1; p_smwords <= 4'd4;
                    p_smoff <= 16'(SM_OFF_V[S_L1_RES_ATTN_PROJ*16 +: 16]);
                    ret_step <= E1_RM1A3; step <= X_SML; sub <= 6'd0; end
    E1_RM1A3: begin p_src <= S_D; p_dst <= S_A; ret_step <= E1_RM1A4; step <= X_DOT; sub <= 6'd0; end
    E1_RM1A4: begin logit0 <= dot_lat; sub <= 6'd0; step <= E1_RM1A5; end
    E1_RM1A5: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_RES_ATTN_NORM_W*16 +: 16]);
                    p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                    ret_step <= E1_RM1A6; step <= X_NORM; sub <= 6'd0; end
    E1_RM1A6: begin p_src <= S_D; p_dst <= S_A; ret_step <= E1_RM1A7; step <= X_DOT; sub <= 6'd0; end
    E1_RM1A7: begin logit1 <= dot_lat; sub <= 6'd0; step <= E1_RM1A8; end
    E1_RM1A8: begin p_dst <= S_X0; p_dst2 <= S_X; p_tmp <= S_X; ret_step <= E1_N1L1; step <= X_RMIX; sub <= 6'd0; end
    E1_N1L1: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_NORM1_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E1_MLAC; step <= X_NORM; sub <= 6'd0; end
    E1_MLAC: begin p_mat <= 7'(M_L1_MLA_WC); p_slo <= S_A; p_shi <= S_A; p_dst <= S_B; p_dhi <= S_C;
                   p_n <= 10'd128; ret_step <= E1_MLAC2; step <= X_GEMV; sub <= 6'd0; end
    E1_MLAC2: begin p_src <= S_B; p_dst <= S_B; p_shi <= S_C; p_dhi <= S_C;
                    p_smoff <= 16'(SM_OFF_V[S_L1_MLA_KNORM_W*16 +: 16]);
                    p_len <= 9'd128; p_log2n <= 5'd7; p_eps <= 24'd42;
                    ret_step <= E1_MLAQ; step <= X_NORM; sub <= 6'd0; end
    E1_MLAQ: begin p_mat <= 7'(M_L1_MLA_WQ_C); p_slo <= S_A; p_shi <= S_A; p_dst <= S_Q;
                   p_n <= 10'd64; ret_step <= E1_MLAQ2; step <= X_GEMV; sub <= 6'd0; end
    E1_MLAQ2: begin p_mat <= 7'(M_L1_MLA_WQ_R); p_slo <= S_A; p_shi <= S_A; p_dst <= S_K;
                    p_n <= 10'd32; ret_step <= E1_MLAK; step <= X_GEMV; sub <= 6'd0; end
    E1_MLAK: begin p_mat <= 7'(M_L1_MLA_WK_C); p_slo <= S_B; p_shi <= S_C; p_dst <= S_D;
                   p_n <= 10'd64; ret_step <= E1_MLAK2; step <= X_GEMV; sub <= 6'd0; end
    E1_MLAK2: begin p_mat <= 7'(M_L1_MLA_WK_R); p_slo <= S_A; p_shi <= S_A; p_dst <= S_F;
                    p_n <= 10'd16; ret_step <= E1_MLAV; step <= X_GEMV; sub <= 6'd0; end
    E1_MLAV: begin p_mat <= 7'(M_L1_MLA_WV); p_slo <= S_B; p_shi <= S_C; p_dst <= S_V;
                   p_n <= 10'd64; ret_step <= E1_KVAPP; step <= X_GEMV; sub <= 6'd0; end
    E1_KVAPP: begin p_tmp <= S_D; ret_step <= E1_ATT; step <= X_KVW; sub <= 6'd0; end
    E1_ATT: begin p_src <= S_Q; p_dst <= S_K; ret_step <= E1_CTX; step <= X_ATTN; sub <= 6'd0; end
    E1_CTX: begin p_mat <= 7'(M_L1_MLA_O); p_slo <= S_C; p_shi <= S_C; p_dst <= S_T;
                   p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E1_PRE2; step <= X_GEMV; sub <= 6'd0; end
    E1_PRE2: begin p_src <= S_P; p_dst <= S_T; p_dst2 <= S_P; ret_step <= E1_RM1B;
                    step <= X_ADDV; sub <= 6'd0; end
    E1_RM1B: begin p_src <= S_X0; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_RES_MLP_NORM_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E1_RM1B2; step <= X_NORM; sub <= 6'd0; end
    E1_RM1B2: begin p_src <= S_A; p_dst <= S_D; p_smkind <= 2'd1; p_smwords <= 4'd4;
                    p_smoff <= 16'(SM_OFF_V[S_L1_RES_MLP_PROJ*16 +: 16]);
                    ret_step <= E1_RM1B3; step <= X_SML; sub <= 6'd0; end
    E1_RM1B3: begin p_src <= S_D; p_dst <= S_A; ret_step <= E1_RM1B4; step <= X_DOT; sub <= 6'd0; end
    E1_RM1B4: begin logit0 <= dot_lat; sub <= 6'd0; step <= E1_RM1B5; end
    E1_RM1B5: begin p_src <= S_P; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_RES_MLP_NORM_W*16 +: 16]);
                    p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                    ret_step <= E1_RM1B6; step <= X_NORM; sub <= 6'd0; end
    E1_RM1B6: begin p_src <= S_D; p_dst <= S_A; ret_step <= E1_RM1B7; step <= X_DOT; sub <= 6'd0; end
    E1_RM1B7: begin logit1 <= dot_lat; sub <= 6'd0; step <= E1_RM1B8; end
    E1_RM1B8: begin p_dst <= S_X0; p_dst2 <= S_P; p_tmp <= S_X; ret_step <= E1_N2L1; step <= X_RMIX; sub <= 6'd0; end
    E1_N2L1: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_L1_NORM2_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E1_MOES; step <= X_NORM; sub <= 6'd0; end
    E1_MOES: begin p_mat <= 7'(M_L1_MOE_ROUTER); p_slo <= S_A; p_shi <= S_A; p_dst <= S_M;
                   p_n <= 10'd8; p_gwide <= 1'b1;
                   ret_step <= E1_MOSG; step <= X_GEMV; sub <= 6'd0; end
    E1_MOSG: begin p_src <= S_M; p_dst <= S_M; ret_step <= E1_MOBI; step <= X_SIG24; sub <= 6'd0; end
    E1_MOS2: begin ret_step <= E1_MOBI; step <= X_SIG24; sub <= 6'd0; end
    E1_MOBI: begin p_dst <= S_B; p_smkind <= 2'd3; p_smwords <= 4'd1;
                   p_smoff <= 16'(SM_OFF_V[S_L1_MOE_BIAS*16 +: 16]);
                   ret_step <= E1_MOB2; step <= X_SML; sub <= 6'd0; end
    E1_MOB2: begin p_op <= 3'd0; p_sh <= 5'd0; p_sc <= 16'sd0; p_src <= S_M; p_dst <= S_B;
                   p_dst2 <= S_G; p_len <= 9'd16; p_wide <= 1'b1;
                   ret_step <= E1_MOTT; step <= X_ALU; sub <= 6'd0; end
    E1_MOTT: begin ret_step <= E1_MG0; step <= X_TOP2; sub <= 6'd0; end
    E1_MG0: begin p_mat <= 7'(moe_shared_mat(M_L1_MOE_ROUTER, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MU0; step <= X_GEMV; sub <= 6'd0; end
    E1_MU0: begin p_mat <= 7'(moe_shared_mat(M_L1_MOE_ROUTER, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MA0; step <= X_GEMV; sub <= 6'd0; end
    E1_MA0: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E1_MM0; step <= X_SILU24; sub <= 6'd0; end
    E1_MM0: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E1_MD0; step <= X_ALU; sub <= 6'd0; end
    E1_MD0: begin p_mat <= 7'(moe_shared_mat(M_L1_MOE_ROUTER, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E1_MZ0; step <= X_GEMV; sub <= 6'd0; end
    E1_MZ0: begin p_src <= S_H; p_sc <= 16'sd8192; ret_step <= E1_MG1; step <= X_MOEACC; sub <= 6'd0; end
    E1_MG1: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel0, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MU1; step <= X_GEMV; sub <= 6'd0; end
    E1_MU1: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel0, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MA1; step <= X_GEMV; sub <= 6'd0; end
    E1_MA1: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E1_MM1; step <= X_SILU24; sub <= 6'd0; end
    E1_MM1: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E1_MD1; step <= X_ALU; sub <= 6'd0; end
    E1_MD1: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel0, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E1_MZ1; step <= X_GEMV; sub <= 6'd0; end
    E1_MZ1: begin p_src <= S_H; p_sc <= mz_w0; ret_step <= E1_MG2; step <= X_MOEACC; sub <= 6'd0; end
    E1_MG2: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel1, 2'd0)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_B; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MU2; step <= X_GEMV; sub <= 6'd0; end
    E1_MU2: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel1, 2'd1)); p_slo <= S_A; p_shi <= S_A;
                   p_dst <= S_D; p_n <= 10'd32; p_gwide <= 1'b1;
                   ret_step <= E1_MA2; step <= X_GEMV; sub <= 6'd0; end
    E1_MA2: begin p_src <= S_B; p_dst <= S_B; p_len <= 9'd32;
                   ret_step <= E1_MM2; step <= X_SILU24; sub <= 6'd0; end
    E1_MM2: begin p_op <= 3'd2; p_sh <= 5'd19; p_wide <= 1'b1; p_b24 <= 1'b1;
                   p_src <= S_B; p_dst <= S_D; p_dst2 <= S_D;
                   p_len <= 9'd32; ret_step <= E1_MD2; step <= X_ALU; sub <= 6'd0; end
    E1_MD2: begin p_mat <= 7'(moe_expert_mat(M_L1_MOE_ROUTER, mz_sel1, 2'd2)); p_slo <= S_D; p_shi <= S_D;
                   p_dst <= S_H; p_n <= 10'd64; p_gwide <= 1'b1;
                   ret_step <= E1_MZ2; step <= X_GEMV; sub <= 6'd0; end
    E1_MZ2: begin p_src <= S_H; p_sc <= mz_w1; ret_step <= E1_MF; step <= X_MOEACC; sub <= 6'd0; end
    E1_MF:  begin p_src <= S_P; p_dst <= S_M; p_dst2 <= S_X; ret_step <= E2_RM;
                  step <= X_ADDV; sub <= 6'd0; end

    // ---------------- output ----------------
    E2_RM:  begin p_src <= S_X0; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_RES_OUT_NORM_W*16 +: 16]);
                  p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                  ret_step <= E2_RM2; step <= X_NORM; sub <= 6'd0; end
    E2_RM2: begin p_src <= S_A; p_dst <= S_D; p_smkind <= 2'd1; p_smwords <= 4'd4;
                   p_smoff <= 16'(SM_OFF_V[S_RES_OUT_PROJ*16 +: 16]);
                   ret_step <= E2_RM3; step <= X_SML; sub <= 6'd0; end
    E2_RM3: begin p_src <= S_D; p_dst <= S_A; ret_step <= E2_RM4; step <= X_DOT; sub <= 6'd0; end
    E2_RM4: begin logit0 <= dot_lat; sub <= 6'd0; step <= E2_RM5; end
    E2_RM5: begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_RES_OUT_NORM_W*16 +: 16]);
                   p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                   ret_step <= E2_RM6; step <= X_NORM; sub <= 6'd0; end
    E2_RM6: begin p_src <= S_D; p_dst <= S_A; ret_step <= E2_RM7; step <= X_DOT; sub <= 6'd0; end
    E2_RM7: begin logit1 <= dot_lat; sub <= 6'd0; step <= E2_RM8; end
    E2_RM8: begin p_dst <= S_X0; p_dst2 <= S_X; p_tmp <= S_X; ret_step <= E2_FN; step <= X_RMIX; sub <= 6'd0; end
    E2_FN:  begin p_src <= S_X; p_dst <= S_A; p_smoff <= 16'(SM_OFF_V[S_FINAL_NORM_W*16 +: 16]);
                  p_len <= 9'd64; p_log2n <= 5'd6; p_eps <= 24'd42;
                  ret_step <= E2_HEAD; step <= X_NORM; sub <= 6'd0; end
    E2_HEAD: begin p_mat <= 7'(M_HEAD); p_slo <= S_A; p_shi <= S_A;
                    p_n <= 10'd512; ret_step <= E2_END; step <= X_GEMV2; sub <= 6'd0; end
    E2_END: begin
      if (sub == 6'd0) begin
        argv <= gv_max; argi <= gv_argi[8:0];
        // Precharge the logits macro read port: word 0 must be on the port in
        // the first drain cycle (address presented one cycle earlier), and the
        // drain keeps the address one beat ahead of the beat counter because
        // the macro read has one cycle of latency at one beat per cycle.
        lg_ra <= 7'd0;
        sub <= 6'd1;
      end
      else begin
        lg_ra <= 7'd1;                       // one-beat read-address lead
        sub <= 6'd0; step <= E0_EMB; phase <= P_DRAIN; drain_beat <= 16'd0;
      end
    end

    // =======================================================================
    // shared executors (implementation in nano_exec.svh)
    // =======================================================================
    `include "nano_exec.svh"

    default: begin
      sub <= 6'd0; step <= E0_EMB;
    end
  endcase
