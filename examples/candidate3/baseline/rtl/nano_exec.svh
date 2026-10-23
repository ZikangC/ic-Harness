// ---------------------------------------------------------------------------
// Shared executors of the token program (included from nano_token.svh, inside
// nano_core's single sequential process).  Every executor is entered with
// sub == 0, drives the leaf units and returns to `ret_step` when finished.
//
// Conventions used below (see PLAN.md section 2 and nano_core.sv):
//   * p_* registers carry the per-step parameters set by the program graph.
//   * VRF writebacks use `mergev(vrf_vc, ...)`: read port C is tied to the
//     write address, so the base is the destination slot's current content.
//   * `shl16(v, l)` moves a vector's lane 0 to lane l before the merge.
//   * macro reads (qz/sc/sm/ck/cv/kk/vv/st) have one cycle of latency: the
//     address is set in cycle t and the data used in cycle t+1.
//   * a_m_small offsets are 16-bit *value* offsets (word = off>>4,
//     lane = off&15), matching tools/pack_image.py.
// ---------------------------------------------------------------------------

    // ---- small-parameter vector load: p_smwords words from p_smoff -> p_dst
    //      kind 0 raw; kind 2 Q2.14 shift (legacy); kind 3 converts the Q4.11
    //      MoE bias to the router's Q5.18 domain (bias24 = bias16*128) ----
    X_SML: begin
      case (sub[0])
        1'b0: begin
          sm_ra <= sm_word_of(p_smoff) + 8'(sub >> 1);
          vrf_wa <= p_dst;
          sub <= sub + 6'd1;
        end
        default: begin
          logic [1023:0] sv;
          logic [511:0]  sl;
          for (int i = 0; i < 64; i++) begin
            if (i < 16) begin
              logic [15:0] v;
              logic signed [63:0] c64;
              v = sm_rd[i*16 +: 16];
              if (p_smkind == 2'd3) begin
                c64 = 64'($signed(v)) * 64'sd256;   // Q4.11 -> Q4.19 code
                sv[i*16 +: 16] = canon_hi(c64);
                sl[i*8 +: 8] = canon_lo(c64);
              end else begin
                sv[i*16 +: 16] = (p_smkind == 2'd2) ? 16'($signed(v) <<< 3) : v;
                sl[i*8 +: 8] = 8'd0;
              end
            end else begin
              sv[i*16 +: 16] = 16'd0;
              sl[i*8 +: 8] = 8'd0;
            end
          end
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16(sv, 7'(16*(sub >> 1))), 7'(16*(sub >> 1)), 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8(sl, 7'(16*(sub >> 1))),
                             7'(16*(sub >> 1)), 7'd16);
          if ((sub >> 1) + 6'd1 >= 6'(p_smwords)) begin sub <= 6'd0; step <= ret_step; end
          else sub <= sub + 6'd1;
        end
      endcase
    end

    // ---- nlin16 pass over 16-lane groups: p_dst = f(p_src) ----
    X_NL: begin
      case (sub[0])
        1'b0: begin
          nl_op <= p_nlop; nl_osh <= p_nlosh; nl_off <= 6'(16*(sub >> 1));
          vrf_ra <= p_src; vrf_wa <= p_dst;
          sub <= sub + 6'd1;
        end
        default: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'(16*(sub >> 1))),
                           7'(16*(sub >> 1)), 7'd16);
          if (9'((sub >> 1) + 6'd1) * 9'd16 >= p_len) begin sub <= 6'd0; step <= ret_step; end
          else sub <= sub + 6'd1;
        end
      endcase
    end

    // ---- alu48 elementwise: p_dst2 = op(p_src, p_dst), 48 lanes per pass ----
    X_ALU: begin
      case (sub[0])
        1'b0: begin
          logic [9:0] rem;
          rem = p_len - 10'(48*(sub >> 1));
          vrf_ra <= p_src; vrf_rb <= p_dst; vrf_wa <= p_dst2;
          alu_op <= p_op; alu_sh <= p_sh; alu_s <= p_sc;
          alu_wide <= p_wide; alu_b24 <= p_b24;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'(48*(sub >> 1)); alu_boff <= 7'(48*(sub >> 1));
          alu_w <= (rem >= 10'd48) ? 3'd2 : (rem >= 10'd32) ? 3'd1 : 3'd0;
          sub <= sub + 6'd1;
        end
        default: begin
          logic [9:0] rem;
          rem = p_len - 10'(48*(sub >> 1));
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'(48*(sub >> 1))),
                           7'(48*(sub >> 1)), (rem >= 10'd48) ? 7'd48 : 7'(rem));
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'(48*(sub >> 1))),
                             7'(48*(sub >> 1)), (rem >= 10'd48) ? 7'd48 : 7'(rem));
          if (10'(48*((sub >> 1) + 6'd1)) >= p_len) begin sub <= 6'd0; step <= ret_step; end
          else sub <= sub + 6'd1;
        end
      endcase
    end

    // ---- 64-lane vector add: p_dst2 = p_src + p_dst ----
    X_ADDV: begin
      p_op <= 3'd0; p_sh <= 5'd0; p_sc <= 16'sd0; p_len <= 9'd64;
      p_wide <= 1'b1;
      step <= X_ALU; sub <= 6'd0;
    end

    // ---- 64-lane dot product p_src . p_dst -> dot_lat (Q4.11) ----
    X_DOT: begin
      if (sub == 6'd0) begin
        vrf_ra <= p_src; vrf_rb <= p_dst;
        oa_src <= 3'd0; ob_src <= 3'd0; oa_off <= 7'd0; ob_off <= 7'd0;
        oa_w <= 3'd3; ob_w <= 3'd3;
        sub <= 6'd1;
      end else begin
        dot_lat <= rne16w(mac_dot_wide, 6'd19);
        sub <= 6'd0; step <= ret_step;
      end
    end

    // ---- res_mix: p_dst2 = p0*p_dst + p1*p_dst2 over the two candidates ----
    X_RMIX: begin
      case (sub)
        6'd0: begin maxlog0 <= (logit0 > logit1) ? logit0 : logit1; sub <= 6'd1; end
        6'd1: begin
          logic signed [15:0] d0, d1;
          logic [15:0] e0v, e1v;
          logic [31:0] s;
          d0 = rne16q(64'(logit0) - 64'(maxlog0), 6'd0);
          d1 = rne16q(64'(logit1) - 64'(maxlog0), 6'd0);
          e0v = nl_exp15(d0);
          e1v = nl_exp15(d1);
          sc_q <= $signed(e0v);
          sc_q2 <= $signed(e1v);
          // the denominator is formed from the same combinational values, not
          // from the just-written sc_q/sc_q2 registers
          s = 32'($unsigned(e0v)) + 32'($unsigned(e1v));
          if (s == 32'd0) s = 32'd1;
          rm_ssum <= s;
          sub <= 6'd2;
        end
        6'd2: begin
          // shared scalar SFU reciprocal: R = 2^30 / (sum of exponentials)
          sfu_op <= 2'd1;
          sfu_din <= {16'd0, rm_ssum};
          sfu_sh <= 6'd30;
          sub <= 6'd3;
        end
        6'd3: begin
          // w = e / sum * 2^14  (Q1.14); R carries 2^30/sum
          w0q <= rne16(48'($signed(sc_q))  * 48'($signed({8'd0, sfu_dout[23:0]})), 6'd16);
          w1q <= rne16(48'($signed(sc_q2)) * 48'($signed({8'd0, sfu_dout[23:0]})), 6'd16);
          sub <= 6'd4;
        end
        // t = rne(p_dst * w0 >> 14) -> S_G (48 + 16 lanes), Q4.19 result
        6'd4: begin
          vrf_ra <= p_dst; vrf_wa <= S_G;
          alu_op <= 3'd3; alu_sh <= 6'd14; alu_s <= w0q; alu_wide <= 1'b1;
          alu_a_src <= 3'd0; alu_aoff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd5;
        end
        6'd5: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd6;
        end
        6'd6: begin
          vrf_ra <= p_dst; vrf_wa <= S_G;
          alu_op <= 3'd3; alu_sh <= 6'd14; alu_s <= w0q; alu_wide <= 1'b1;
          alu_aoff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd7;
        end
        6'd7: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd8;
        end
        // t2 = rne(p_dst2 * w1 >> 14) -> S_H
        6'd8: begin
          vrf_ra <= p_dst2; vrf_wa <= S_H;
          alu_op <= 3'd3; alu_sh <= 6'd14; alu_s <= w1q; alu_wide <= 1'b1;
          alu_aoff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd9;
        end
        6'd9: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd10;
        end
        6'd10: begin
          vrf_ra <= p_dst2; vrf_wa <= S_H;
          alu_op <= 3'd3; alu_sh <= 6'd14; alu_s <= w1q; alu_wide <= 1'b1;
          alu_aoff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd11;
        end
        6'd11: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd12;
        end
        // p_tmp = t + t2 (the block/prefix candidates are consumed above)
        6'd12: begin
          vrf_ra <= S_G; vrf_rb <= S_H; vrf_wa <= p_tmp;
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_s <= 16'sd0; alu_wide <= 1'b1;
          alu_aoff <= 7'd0; alu_boff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd13;
        end
        6'd13: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd14;
        end
        6'd14: begin
          vrf_ra <= S_G; vrf_rb <= S_H; vrf_wa <= p_tmp;
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_wide <= 1'b1;
          alu_aoff <= 7'd48; alu_boff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd15;
        end
        6'd15: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd0; step <= ret_step;
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- RMSNorm over p_len lanes in blocks of 2^p_log2n; the per-lane gain
    //      (Q1.14) is folded with G = rne(2^25/sqrt(mean+eps)) ----
    X_NORM, X_NORMH: begin
      case (sub)
        6'd0, 6'd2, 6'd4, 6'd6, 6'd8, 6'd10, 6'd12, 6'd14: begin
          sm_ra <= sm_word_of(p_smoff) + 8'(sub >> 1);
          vrf_wa <= ((sub >> 1) < 6'd4) ? S_G : S_H;
          sub <= sub + 6'd1;
        end
        6'd1, 6'd3, 6'd5, 6'd7, 6'd9, 6'd11, 6'd13, 6'd15: begin
          logic [1023:0] sv;
          for (int i = 0; i < 64; i++) sv[i*16 +: 16] = (i < 16) ? sm_rd[i*16 +: 16] : 16'd0;
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16(sv, 7'(16*((sub >> 1) & 6'd3))),
                           7'(16*((sub >> 1) & 6'd3)), 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'(16*((sub >> 1) & 6'd3)), 7'd16);
          if ((sub == 6'd7 && p_len <= 9'd64) || sub == 6'd15) begin
            sub <= 6'd16; nm_blk <= 8'd0;
          end else sub <= sub + 6'd1;
        end
        6'd16: begin
          vrf_ra <= 4'(p_src + 4'(nm_blk >> 1));
          red_op <= 2'd1;
          red_win <= (p_len == 9'd128) ? 2'd0
                   : ((p_len == 9'd64 && p_log2n == 5'd5) ? (2'd1 + 2'(nm_blk)) : 2'd0);
          sub <= 6'd17;
        end
        6'd17: begin
          if (p_len == 9'd128) begin
            nm_ss <= red_sq; vrf_ra <= p_shi; sub <= 6'd18;
          end else begin
            nm_ss <= red_sq; sub <= 6'd19;
          end
        end
        6'd18: begin nm_ss <= nm_ss + red_sq; sub <= 6'd19; end
        6'd19: begin
          logic signed [55:0] mss;
          logic [47:0] m48;
          // mean of the Q4.38 sum of squares, in Q4.22 units (the eps scale)
          mss = (nm_ss + (56'sd1 << (p_log2n - 6'd1))) >>> p_log2n;
          mss = (mss + 56'sd32768) >>> 16;
          m48 = 48'(mss) + 48'(p_eps);
          nm_m <= m48;
          nm_drop <= rsqrt_drop(m48);
          sfu_op <= 2'd0;
          sfu_din <= m48;
          sub <= 6'd20;
        end
        6'd20: begin
          nm_g <= sfu_dout;
          nm_i <= 8'(nm_blk * 8'(9'd1 << (p_log2n - 5'd4)));   // first group of this block
          sub <= 6'd21;
        end
        // per 16-lane group: fold the gain with this block's G, then apply.
        // With two blocks (p_len 64, d 32) the folded factors go to S_H so the
        // original gains in S_G stay available for the second block.
        6'd21: begin
          logic nb2, hi;
          logic [47:0] gsh;
          nb2 = (p_len == 9'd64) && (p_log2n == 5'd5);
          hi  = (nb2) ? 1'b0 : (nm_i >= 8'd4);
          gsh = nm_g >>> nm_drop;
          vrf_ra <= hi ? S_H : S_G;
          // nb2 runs with p_src == S_H (the KDA head vector), so the folded
          // gains must not be written there: S_C is free at this point and is
          // also the scratch used by the following X_SIGMUL.
          vrf_wa <= nb2 ? S_C : (hi ? S_H : S_G);
          alu_op <= 3'd3; alu_sh <= 6'd14;
          alu_s <= (gsh > 48'sd32767) ? 16'sh7FFF : $signed(gsh[15:0]);
          alu_wide <= 1'b0;
          // nb2 normalises two 32-lane heads with the same 32 gains: the
          // gain groups repeat every two groups (0,1,0,1), not 0..3.
          alu_a_src <= 3'd0;
          alu_aoff <= 7'(16*((nb2 ? (nm_i & 8'd1) : nm_i) & 8'd3));
          alu_w <= 3'd0;
          sub <= 6'd22;
        end
        6'd22: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'(16*(nm_i & 8'd3))),
                           7'(16*(nm_i & 8'd3)), 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'(16*(nm_i & 8'd3)), 7'd16);
          sub <= 6'd23;
        end
        6'd23: begin
          logic nb2, hi, so;
          nb2 = (p_len == 9'd64) && (p_log2n == 5'd5);
          hi  = (nb2) ? 1'b0 : (nm_i >= 8'd4);
          so  = ((p_len == 9'd128) && (nm_i >= 8'd4)) ? 1'b1 : 1'b0;
          vrf_ra <= so ? p_shi : p_src;
          vrf_rb <= nb2 ? S_C : ((nb2 | hi) ? S_H : S_G);
          vrf_wa <= so ? p_dhi : p_dst;
          alu_op <= 3'd2; alu_sh <= 5'(6'd14 - {1'b0, nm_drop}); alu_s <= 16'sd0;
          alu_wide <= 1'b1; alu_b24 <= 1'b0;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'(16*(nm_i & 8'd3)); alu_boff <= 7'(16*(nm_i & 8'd3));
          alu_w <= 3'd0;
          sub <= 6'd24;
        end
        6'd24: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'(16*(nm_i & 8'd3))),
                           7'(16*(nm_i & 8'd3)), 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'(16*(nm_i & 8'd3))),
                             7'(16*(nm_i & 8'd3)), 7'd16);
          if (nm_i + 8'd1 >= 8'((nm_blk + 8'd1) * 8'(9'd1 << (p_log2n - 5'd4)))) begin
            if (nm_blk + 8'd1 >= 8'(p_len >> p_log2n)) begin sub <= 6'd0; step <= ret_step; end
            else begin nm_blk <= nm_blk + 8'd1; sub <= 6'd16; end
          end else begin nm_i <= nm_i + 8'd1; sub <= 6'd21; end
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- L2Norm of two 32-lane head blocks (in place, p_src) ----
    X_L2: begin
      case (sub)
        6'd0: begin nm_blk <= 8'd0; alu_wide <= 1'b0; sub <= 6'd1; end
        6'd1: begin
          vrf_ra <= p_src; red_op <= 2'd1;
          red_win <= (nm_blk == 8'd0) ? 2'd1 : 2'd2;
          sub <= 6'd2;
        end
        6'd2: begin
          // Q4.19 input: the sum of squares is in 2^38 units, so eps=1e-6 is
          // 274878.  With M in 2^38..2^46 the unscaled 2^25/sqrt(M) is only
          // ~2^4, so returning it as an integer quantises the L2 unit-vector
          // gain by >1% (measured 1.4% high on head0 and 6.4% low on head1,
          // which dominated the KDA error).  Pre-scale M by 2^(2*s2) from its
          // leading bit so the returned gain is ~2^14, and carry the 2^s2
          // compensation in the ALU product shift.
          logic [55:0] m56;
          logic [5:0]  l56;
          logic [4:0]  s2v;
          m56 = red_sq + 56'sd274878;
          l56 = msb56(m56);
          s2v = (l56 >= 6'd21) ? 5'((l56 - 6'd21) >> 1) : 5'd0;
          nm_ss <= m56;
          nm_drop <= s2v;
          sfu_op <= 2'd0;
          sfu_din <= 48'(m56 >> (2*s2v));
          sub <= 6'd3;
        end
        6'd3: begin nm_g <= sfu_dout; sub <= 6'd4; end
        6'd4: begin
          logic [47:0] gsh;
          logic [5:0]  lg;
          logic [4:0]  dd;
          lg = msb48(nm_g);
          dd = (lg > 6'd14) ? 5'(lg - 6'd14) : 5'd0;
          gsh = nm_g >>> dd;
          vrf_ra <= p_src; vrf_wa <= p_src;
          // L2 output format: Q1.14 (unit vector, |x|<=1) instead of Q4.11.
          // The input is the 24-bit Q4.19 conv output, so the multiply is wide.
          // gsh ~ 2^s2 * G_exact / 2^dd, so the product shift is 11+s2-dd.
          alu_op <= 3'd3;
          alu_sh <= 5'(6'd11 + {1'b0, nm_drop} - {1'b0, dd});
          alu_wide <= 1'b1;
          alu_s <= (gsh > 48'sd32767) ? 16'sh7FFF : $signed(gsh[15:0]);
          alu_a_src <= 3'd0; alu_aoff <= 7'(32*(nm_blk[0])); alu_w <= 3'd1;
          sub <= 6'd5;
        end
        6'd5: begin
          logic [511:0] l2v;
          // The L2 result is a signed Q1.14 code carried as the ALU's 24-bit
          // canonical split (|code| <= 2^14).  The VRF word must be the exact
          // two's-complement 16-bit code: concatenating the high word's low
          // byte with the *signed* remainder byte would zero-extend it and add
          // +256 codes whenever the remainder is negative (measured as a
          // +0.0156 (= 2^-6) per-lane bias on k and q).
          for (int i = 0; i < 32; i++) begin
            logic signed [23:0] c24;
            c24 = join24(alu_y[i*16 +: 16], alu_y_lo[i*8 +: 8]);
            l2v[i*16 +: 16] = c24[15:0];
          end
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({512'd0, l2v}, 7'(32*(nm_blk[0]))),
                           7'(32*(nm_blk[0])), 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'(32*(nm_blk[0])), 7'd32);
          if (nm_blk == 8'd0) begin nm_blk <= 8'd1; sub <= 6'd1; end
          else begin sub <= 6'd0; step <= ret_step; end
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- KDA decay gate: alpha = exp(-5*sigmoid(A[head]*(f + dtb))) in Q1.15,
    //      written to S_D; S_F holds f, the small macro holds dtb and A ----
    X_ALPHA: begin
      case (sub)
        6'd0, 6'd2, 6'd4, 6'd6: begin
          sm_ra <= sm_word_of(16'(SM_OFF_V[S_L0_KDA_DTB*16 +: 16])) + 8'(sub >> 1);
          vrf_wa <= S_G;
          alu_wide <= 1'b0;
          sub <= sub + 6'd1;
        end
        6'd1, 6'd3, 6'd5, 6'd7: begin
          logic [1023:0] sv;
          for (int i = 0; i < 64; i++) sv[i*16 +: 16] = (i < 16) ? sm_rd[i*16 +: 16] : 16'd0;
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16(sv, 7'(16*(sub >> 1))), 7'(16*(sub >> 1)), 7'd16);
          if (sub == 6'd7) begin
            sm_ra <= sm_word_of(16'(SM_OFF_V[S_L0_KDA_A*16 +: 16]));
            sub <= 6'd8;
          end else sub <= sub + 6'd1;
        end
        6'd8: begin
          al_a0 <= $signed(sm_rd[0 +: 16]);
          al_a1 <= $signed(sm_rd[16 +: 16]);
          sub <= 6'd9;
        end
        // t = f + dtb -> S_D
        6'd9: begin
          vrf_ra <= S_F; vrf_rb <= S_G; vrf_wa <= S_D;
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_s <= 16'sd0;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'd0; alu_boff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd10;
        end
        6'd10: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd11;
        end
        6'd11: begin
          vrf_ra <= S_F; vrf_rb <= S_G; vrf_wa <= S_D;
          alu_op <= 3'd0; alu_aoff <= 7'd48; alu_boff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd12;
        end
        6'd12: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd13;
        end
        // t = t * A[head] (Q8.8) -> S_D, per head block
        6'd13: begin
          vrf_ra <= S_D; vrf_wa <= S_D;
          alu_op <= 3'd3; alu_sh <= 6'd8; alu_s <= al_a0;
          alu_aoff <= 7'd0; alu_w <= 3'd1;
          sub <= 6'd14;
        end
        6'd14: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd32);
          sub <= 6'd15;
        end
        6'd15: begin
          vrf_ra <= S_D; vrf_wa <= S_D;
          alu_op <= 3'd3; alu_sh <= 6'd8; alu_s <= al_a1;
          alu_aoff <= 7'd32; alu_w <= 3'd1;
          sub <= 6'd16;
        end
        6'd16: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd32), 7'd32, 7'd32);
          sub <= 6'd17;
        end
        // sigmoid (4 x 16 lanes)
        6'd17: begin nl_op <= 2'd1; nl_osh <= 5'd15; nl_off <= 6'd0;  vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd18; end
        6'd18: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd0),  7'd0,  7'd16); sub <= 6'd19; end
        6'd19: begin nl_op <= 2'd1; nl_off <= 6'd16; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd20; end
        6'd20: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd16), 7'd16, 7'd16); sub <= 6'd21; end
        6'd21: begin nl_op <= 2'd1; nl_off <= 6'd32; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd22; end
        6'd22: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd32), 7'd32, 7'd16); sub <= 6'd23; end
        6'd23: begin nl_op <= 2'd1; nl_off <= 6'd48; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd24; end
        6'd24: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd48), 7'd48, 7'd16); sub <= 6'd25; end
        // arg = -5 * sigmoid  (Q1.15 -> Q4.11)
        6'd25: begin
          vrf_ra <= S_D; vrf_wa <= S_D;
          alu_op <= 3'd3; alu_sh <= 6'd15; alu_s <= -16'sd10240;
          alu_aoff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd26;
        end
        6'd26: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd27;
        end
        6'd27: begin
          vrf_ra <= S_D; vrf_wa <= S_D;
          alu_aoff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd28;
        end
        6'd28: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd29;
        end
        // exp (4 x 16 lanes, Q1.15 out)
        6'd29: begin nl_op <= 2'd0; nl_osh <= 5'd15; nl_off <= 6'd0;  vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd30; end
        6'd30: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd0),  7'd0,  7'd16); sub <= 6'd31; end
        6'd31: begin nl_off <= 6'd16; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd32; end
        6'd32: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd16), 7'd16, 7'd16); sub <= 6'd33; end
        6'd33: begin nl_off <= 6'd32; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd34; end
        6'd34: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd32), 7'd32, 7'd16); sub <= 6'd35; end
        6'd35: begin nl_off <= 6'd48; vrf_ra <= S_D; vrf_wa <= S_D; sub <= 6'd36; end
        6'd36: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd48), 7'd48, 7'd16);
                      sub <= 6'd0; step <= ret_step; end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- KDA state recurrence for head p_head (S_D = alpha Q1.15,
    //      S_K = l2(k) Q1.14, S_Q = l2(q) Q1.14, S_V = v Q4.12, S_G = u Q4.19,
    //      S_H[32*p_head] = head output Q1.18).  The stored state S is the
    //      24-bit Q4.19 split (value = (hi*256+lo)*2^-19) held in a 64x768
    //      macro word: high plane [511:0], signed low plane [767:512]. ----
    X_STATE: begin
      case (sub)
        // phase 1: S[r][:] = alpha[r] * S[r][:]  (alpha is a per-row vector in
        // S_D as Q1.15, head h in lanes 32h..32h+31; the scalar of row r is
        // selected combinationally from VRF port D, so no extra latency stage
        // separates the row address from the ALU result)
        6'd0: begin
          vrf_rd <= S_D;
          st_row <= 8'd0;
          alu_wide <= 1'b0;
          sub <= 6'd1;
        end
        6'd1: begin
          st_ra <= 7'(32*p_head + st_row[6:0]);
          alu_soff <= 7'(32*p_head + st_row[5:0]);
          alu_s_vd <= 1'b1;
          sub <= 6'd56;                       // one-cycle macro read wait
        end
        6'd56: begin sub <= 6'd2; end        // address(row r) presented -> data at 2
        6'd2: begin
          // 24-bit state: alpha (Q1.15) x cS (Q4.19), shift 15 -> Q4.19
          alu_op <= 3'd3; alu_sh <= 6'd15; alu_wide <= 1'b1;
          alu_a_src <= 3'd1; alu_aoff <= 7'd0; alu_w <= 3'd1;
          ow_off <= 7'd0; ow_w <= 3'd1;
          sub <= 6'd3;
        end
        6'd3: begin
          st_we <= 1'b1; st_wa <= 7'(32*p_head + st_row[6:0]);
          // 768-bit word: high Q4.11 plane [511:0], signed low plane [767:512]
          st_wd <= {alu_y_lo[255:0], alu_y[511:0]};
          if (st_row == 8'd31) begin st_row <= 8'd0; sub <= 6'd4; end
          else begin st_row <= st_row + 8'd1; sub <= 6'd1; end
        end
        // phase 2: k @ S
        6'd4: begin
          mac_clr <= 1'b1; mac_en <= 1'b1; mac_wmode <= 1'b1;
          // 24-bit state: (k*2^14)*(S*2^19) >> 14 = Q4.19 (v is Q4.19 now)
          mac_scl <= 24'd1; mac_sh <= 6'd14;
          ow_off <= 7'd0; ow_w <= 3'd1;
          sub <= 6'd5;
        end
        6'd5: begin
          st_ra <= 7'(32*p_head + st_row[6:0]);
          vrf_rb <= S_K;
          oa_src <= 3'd1; oa_w <= 3'd1; oa_off <= 7'd0;
          ob_src <= 3'd7; ob_blane <= 7'(32*p_head + st_row[6:0]);
          sub <= 6'd55;                       // one-cycle macro read wait
        end
        6'd55: begin sub <= 6'd6; end        // address(row r) presented -> data at 6
        6'd6: begin
          mac_en <= 1'b1;
          if (st_row == 8'd31) sub <= 6'd7;
          else begin st_row <= st_row + 8'd1; sub <= 6'd5; end
        end
        6'd7: begin vrf_wa <= S_G; sub <= 6'd8; end
        6'd8: begin
          logic [511:0] kh;
          logic [255:0] kl;
          for (int i = 0; i < 32; i++) begin
            kh[i*16 +: 16] = mac_acc_wout[i*24 + 8 +: 16];
            kl[i*8 +: 8]   = mac_acc_wout[i*24 +: 8];
          end
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, {512'd0, kh}, 7'd0, 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, {256'd0, kl}, 7'd0, 7'd32);
          sub <= 6'd9;
        end
        // u = v - k @ S -> S_G, expressed as the canonical Q4.19 code:
        // v and k@S are 16-bit Q4.12 words; as 24-bit operands they carry
        // value*2^20, so a wide subtract with shift 1 lands in Q4.19.
        6'd9: begin
          vrf_ra <= S_V; vrf_rb <= S_G; vrf_wa <= S_G;
          alu_op <= 3'd1; alu_sh <= 6'd0; alu_s <= 16'sd0;
          alu_wide <= 1'b1;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'(32*p_head); alu_boff <= 7'd0; alu_w <= 3'd1;
          sub <= 6'd10;
        end
        6'd10: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd32);
          sub <= 6'd11;
        end
        // phase 3: rank-1 update S[row][:] += delta, delta = u * (beta*k)[row]
        //   11  load beta*k into S_C (lanes 0..31, Q1.14)
        //   12  select scalar, 13 delta Q4.19, 14 write S_B, 15 add, 16 commit
        6'd11: begin
          vrf_ra <= S_K; vrf_wa <= S_C;
          alu_op <= 3'd3; alu_sh <= 6'd15; alu_wide <= 1'b0;
          alu_s <= (p_head == 2'd0) ? beta0 : beta1;
          alu_aoff <= 7'(32*p_head); alu_w <= 3'd1; alu_a_src <= 3'd0;
          alu_s_vd <= 1'b0;
          st_row <= 8'd0;
          sub <= 6'd12;
        end
        6'd12: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'd0, 7'd32);
          vrf_rd <= S_C;                       // (beta*k)[row] on port D
          sub <= 6'd13;
        end
        // row loop: delta = u(Q4.19) * (beta*k)[row](Q1.14) >> 14 -> Q4.19
        6'd13: begin
          vrf_ra <= S_G;                       // u, 24-bit Q4.19
          vrf_rb <= S_B;                       // delta row (written at 14)
          alu_soff <= 7'(st_row[5:0]);
          alu_s_vd <= 1'b1;
          alu_op <= 3'd3; alu_sh <= 6'd14; alu_wide <= 1'b1;
          alu_a_src <= 3'd0; alu_aoff <= 7'd0; alu_w <= 3'd1;
          st_ra <= 7'(32*p_head + st_row[6:0]);
          sub <= 6'd14;
        end
        6'd14: begin
          vrf_wa <= S_B; vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd32);
          sub <= 6'd15;
        end
        6'd15: begin                            // S_B write lands here
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_wide <= 1'b1;
          alu_a_src <= 3'd1; alu_aoff <= 7'd0; alu_w <= 3'd1;
          alu_b_src <= 3'd0; alu_boff <= 7'd0;
          sub <= 6'd16;
        end
        6'd16: begin
          st_we <= 1'b1; st_wa <= 7'(32*p_head + st_row[6:0]);
          st_wd <= {alu_y_lo[255:0], alu_y[511:0]};
          if (st_row == 8'd31) begin st_row <= 8'd0; sub <= 6'd17; end
          else begin st_row <= st_row + 8'd1; sub <= 6'd13; end
        end
        // phase 4: out = (q @ S) / sqrt(32) -> S_H[32*p_head]
        //   q is Q1.14 and the state is 24-bit Q4.19, so (q*2^14)*(S*2^19)
        //   >> 18 is 2^15*q@S (Q1.15); the 362/2^8 multiply below then keeps
        //   the stored Q1.18 readout code unchanged.
        6'd17: begin
          mac_clr <= 1'b1; mac_en <= 1'b1; mac_wmode <= 1'b1;
          mac_scl <= 24'd1; mac_sh <= 6'd18;
          ow_off <= 7'd0; ow_w <= 3'd1;
          st_row <= 8'd0;
          alu_s_vd <= 1'b0;
          sub <= 6'd18;
        end
        6'd18: begin
          st_ra <= 7'(32*p_head + st_row[6:0]);
          vrf_rb <= S_Q;
          oa_src <= 3'd1; oa_w <= 3'd1; oa_off <= 7'd0;
          ob_src <= 3'd7; ob_blane <= 7'(32*p_head + st_row[6:0]);
          sub <= 6'd54;                       // one-cycle macro read wait
        end
        6'd54: begin sub <= 6'd19; end       // address(row r) presented -> data at 19
        6'd19: begin
          mac_en <= 1'b1;
          if (st_row == 8'd31) sub <= 6'd20;
          else begin st_row <= st_row + 8'd1; sub <= 6'd18; end
        end
        6'd20: begin vrf_wa <= S_B; sub <= 6'd21; end
        6'd21: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, {512'd0, mac_acc_out[511:0]}, 7'd0, 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'd0, 7'd32);
          sub <= 6'd22;
        end
        6'd22: begin
          vrf_ra <= S_B; vrf_wa <= S_H;
          // The S_B readout accumulator is a 16-bit Q1.15 word (the wide MAC
          // output); this scaling stays in the 16-bit ALU path, exactly as the
          // previous Q1.14-state implementation did.
          alu_op <= 3'd3; alu_sh <= 6'd8; alu_s <= 16'sd362;   // 2048/sqrt(32)
          alu_wide <= 1'b0;
          alu_aoff <= 7'd0; alu_w <= 3'd1; alu_a_src <= 3'd0;
          sub <= 6'd23;
        end
        6'd23: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'(32*p_head)), 7'(32*p_head), 7'd32);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'(32*p_head), 7'd32);
          sub <= 6'd24;
        end
        6'd24: begin sub <= 6'd0; step <= ret_step; end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- KDA output gate: p_src = p_src * sigmoid(p_dst) ----
    X_SIGMUL: begin
      case (sub)
        6'd0: begin nl_op <= 2'd1; nl_osh <= 5'd15; nl_off <= 6'd0;  vrf_ra <= p_dst; vrf_wa <= S_C;
                     alu_wide <= 1'b1; sub <= 6'd1; end
        6'd1: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd0),  7'd0,  7'd16); sub <= 6'd2; end
        6'd2: begin nl_off <= 6'd16; vrf_ra <= p_dst; vrf_wa <= S_C; sub <= 6'd3; end
        6'd3: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd16), 7'd16, 7'd16); sub <= 6'd4; end
        6'd4: begin nl_off <= 6'd32; vrf_ra <= p_dst; vrf_wa <= S_C; sub <= 6'd5; end
        6'd5: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd32), 7'd32, 7'd16); sub <= 6'd6; end
        6'd6: begin nl_off <= 6'd48; vrf_ra <= p_dst; vrf_wa <= S_C; sub <= 6'd7; end
        6'd7: begin vrf_we <= 1'b1; vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'd48), 7'd48, 7'd16); sub <= 6'd8; end
        // v = v * gate (Q1.15) in place
        6'd8: begin
          vrf_ra <= p_src; vrf_rb <= S_C; vrf_wa <= p_src;
          alu_op <= 3'd2; alu_sh <= 6'd15; alu_s <= 16'sd0; alu_wide <= 1'b1; alu_b24 <= 1'b0;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'd0; alu_boff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd9;
        end
        6'd9: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
           vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd10;
        end
        6'd10: begin
          vrf_ra <= p_src; vrf_rb <= S_C; vrf_wa <= p_src;
          alu_aoff <= 7'd48; alu_boff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd11;
        end
        6'd11: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
           vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd0; step <= ret_step;
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- MLA key/value cache append at the current position ----
    X_KVW: begin
      case (sub)
        6'd0: begin
          vrf_ra <= p_tmp; vrf_rb <= S_F; vrf_rd <= S_V;
          kk_wa <= cpos[5:0]; vv_wa <= cpos[5:0];
          sub <= 6'd1;
        end
        6'd1: begin
          kk_we <= 1'b1; kk_wstrb <= {192{1'b1}}; kk_wd <= asm_key(vrf_va, vrf_vb);
          vv_we <= 1'b1; vv_wstrb <= {128{1'b1}}; vv_wd <= vrf_vd;
          cpos <= cpos + 7'd1;
          sub <= 6'd0; step <= ret_step;
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- causal MLA attention (both heads) -> contexts in S_C.
    //      p_src = qc (2x32), p_dst = qr (2x16); at_ph[2] selects the head.
    //      Softmax is computed in Q4.11 scores / Q1.15 exponents; the context
    //      is sum(e*value)/sum(e) in Q4.11. ----
    X_ATTN: begin
      case (sub)
        6'd0: begin at_ph <= 8'd0; alu_wide <= 1'b0; sub <= 6'd1; end
        // setup: query vector -> S_G, score slot S_B = -32768
        6'd1: begin
          vrf_ra <= p_src; vrf_rb <= p_dst; vrf_wa <= S_G;
          sub <= 6'd2;
        end
        6'd2: begin
          // The VRF registers we/wa/wd together and commits one cycle later,
          // so the destination must not change in the same cycle as the data.
          // wa stays S_G here (set at sub1); the S_B pointer moves at sub3.
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, asm_q(vrf_va, vrf_vb, at_ph[2]), 7'd0, 7'd48);
          sub <= 6'd3;
        end
        6'd3: begin
          vrf_we <= 1'b1; vrf_wd <= {64{16'sh8000}}; vrf_wa <= S_B;
          sub <= 6'd4;
        end
        6'd4: begin at_pos <= 8'd0; sub <= 6'd5; end
        // scores: 4 cycles per position
        6'd5: begin
          kk_ra <= at_pos[5:0];
          vrf_ra <= S_G;
          oa_src <= 3'd0; oa_w <= 3'd3; oa_off <= 7'd0;
          ob_src <= 3'd2; ob_w <= 3'd2; ob_off <= 7'(48*at_ph[2]);
          sub <= 6'd6;
        end
        6'd6: begin
          at_dot <= mac_dot;
          sfu_op <= 2'd2; sfu_din <= mac_dot; sfu_din2 <= 48'sd591; sfu_sh <= 6'd23;
          vrf_wa <= S_B;
          sub <= 6'd7;
        end
        6'd7: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({1008'd0, rne16q($signed(sfu_dout), 6'd0)}, 7'(at_pos)),
                           7'(at_pos), 7'd1);
          sub <= 6'd8;
        end
        6'd8: begin
          // cpos is the number of cached positions (it was advanced when this
          // position's key/value row was appended), so the causal window is
          // 0 .. cpos-1
          if (at_pos[6:0] + 7'd1 == cpos) sub <= 6'd9;
          else begin at_pos <= at_pos + 8'd1; sub <= 6'd5; end
        end
        // softmax: max -> m-vector in S_D -> (score-max) in S_D -> exp in S_B.
        // The subtraction result must be committed to S_D before the exponent
        // lane reads it, so each 16-lane group takes three cycles.
        6'd9:  begin vrf_ra <= S_B; red_op <= 2'd2; sub <= 6'd10; end
        6'd10: begin at_mx <= $signed(red_y[15:0]); vrf_wa <= S_D; sub <= 6'd11; end
        6'd11: begin vrf_we <= 1'b1; vrf_wd <= {64{at_mx}}; sub <= 6'd12; end
        6'd12: begin at_pos <= 8'd0; sub <= 6'd13; end
        6'd13: begin
          vrf_ra <= S_B; vrf_rb <= S_D;
          alu_op <= 3'd1; alu_sh <= 6'd0; alu_s <= 16'sd0;
          alu_a_src <= 3'd0; alu_b_src <= 3'd0;
          alu_aoff <= 7'(16*at_pos[1:0]); alu_boff <= 7'(16*at_pos[1:0]);
          alu_w <= 3'd0;
          vrf_wa <= S_D;
          sub <= 6'd14;
        end
        6'd14: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'(16*at_pos[1:0])),
                           7'(16*at_pos[1:0]), 7'd16);
          sub <= 6'd15;
        end
        6'd15: begin                       // S_D now holds score-max
          nl_op <= 2'd0; nl_osh <= 5'd15; nl_off <= 6'(16*at_pos[1:0]);
          vrf_ra <= S_D; vrf_wa <= S_B;
          sub <= 6'd16;
        end
        6'd16: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({768'd0, nl_y}, 7'(16*at_pos[1:0])),
                           7'(16*at_pos[1:0]), 7'd16);
          if (at_pos >= 8'd3) sub <= 6'd17;
          else begin at_pos <= at_pos + 8'd1; sub <= 6'd13; end
        end
        6'd17: begin vrf_ra <= S_B; red_op <= 2'd0; sub <= 6'd18; end
        6'd18: begin
          at_tot <= red_y;
          sfu_op <= 2'd1; sfu_din <= red_y; sfu_sh <= 6'd30;
          sub <= 6'd19;
        end
        6'd19: begin sfu_r <= sfu_dout; sub <= 6'd20; end
        // context accumulation: acc[c] += e[p] * value[p][c]
        6'd20: begin
          mac_clr <= 1'b1; mac_en <= 1'b1; mac_wmode <= 1'b0;
          mac_scl <= 24'd1; mac_sh <= 6'd0;
          ow_off <= 7'd0; ow_w <= 3'd1;
          at_pos <= 8'd0;
          sub <= 6'd21;
        end
        6'd21: begin
          vv_ra <= at_pos[5:0]; vrf_rb <= S_B;
          oa_src <= 3'd3; oa_w <= 3'd1; oa_off <= 7'(32*at_ph[2]);
          ob_src <= 3'd7; ob_blane <= 7'(at_pos);
          sub <= 6'd22;
        end
        6'd22: begin
          mac_en <= 1'b1;
          if (at_pos[6:0] + 7'd1 == cpos) sub <= 6'd23;
          else begin at_pos <= at_pos + 8'd1; sub <= 6'd21; end
        end
        // ctx[c] = rne(acc[c] * R >> 30) with R = rne(2^30/sum):
        //   acc = sum(e_q15 * v_q411) = 2^26 * sum(e*v)
        //   R   = 2^30 / (2^15 * sum(e)) = 2^15 / sum(e)
        //   ctx = 2^26 * 2^15 / 2^30 * sum(p*v) = 2^11 * sum(p*v)  (Q4.11)
        6'd23: begin at_pos <= 8'd0; sub <= 6'd24; end
        6'd24: begin
          sfu_op <= 2'd2;
          // sign-extend the lane accumulator: a zero-extended negative value
          // would saturate the context lane
          sfu_din <= 48'($signed(mac_acc[56*at_pos[5:0] +: 56]));
          sfu_din2 <= sfu_r;
          sfu_sh <= 6'd30;
          vrf_wa <= S_C;
          sub <= 6'd25;
        end
        6'd25: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({1008'd0, rne16q($signed(sfu_dout), 6'd0)},
                                         7'(32*at_ph[2] + 7'(at_pos[5:0]))),
                           7'(32*at_ph[2] + 7'(at_pos[5:0])), 7'd1);
          vrf_lwd <= mergev8(vrf_lvc, 512'd0, 7'(32*at_ph[2] + 7'(at_pos[5:0])), 7'd1);
          if (at_pos == 8'd31) sub <= 6'd26;
          else begin at_pos <= at_pos + 8'd1; sub <= 6'd24; end
        end
        6'd26: begin
          if (at_ph[2] == 1'b0) begin at_ph <= 8'd4; sub <= 6'd1; end
          else begin sub <= 6'd0; step <= ret_step; end
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- GEMV: p_dst[+1] = W[p_mat] @ {p_slo, p_shi}, p_n columns ----
    X_GEMV: begin
      // One output column per cycle after the single-cycle macro-read fill.
      // The result row is accumulated in the gv_row/gv_rowl shadow registers
      // (one lane per cycle) and committed to the VRF destination in one
      // masked write, so the per-column read-modify-write that forced a
      // 4-cycle stride is gone.  Cycle t presents the packed-weight and scale
      // addresses of column c and consumes the data of column c-1.
      case (sub)
        6'd0: begin
          gv_acc <= 56'sd0;
          gv_wide <= p_gwide;
          pv_col <= 10'd0; pv_kb <= 2'd0;
          if (per_col(p_mat) > 2'd1) begin gv_col <= 10'd0; gv_kb <= 2'd1; end
          else begin gv_col <= 10'd1; gv_kb <= 2'd0; end
          vrf_ra <= p_slo;
          gv_row <= 1024'd0; gv_rowl <= 512'd0;
          gv_rowh <= 1024'd0; gv_rowhl <= 512'd0;
          oa_src <= 3'd0; oa_w <= matw(p_mat); oa_off <= 7'd0;
          ob_src <= 3'd6; ob_w <= matw(p_mat); ob_off <= 7'd0;
          sub <= 6'd1;
        end
        6'd1: begin
          logic signed [15:0] scv;
          logic signed [55:0] pterm, acc_new;
          logic               last_word;
          logic [9:0]         nc;
          logic [1:0]         nk;
          scv = $signed(sc_rd[16*(16'(sc_slot(p_mat, pv_col, pv_kb)) % 16'd10) +: 16]);
          pterm = mac_dot_wide * scv;
          acc_new = (pv_kb == 2'd0) ? pterm : gv_acc + pterm;
          last_word = (pv_col + 10'd1 == p_n) && (pv_kb + 2'd1 == per_col(p_mat));
          if (pv_kb + 2'd1 == per_col(p_mat)) begin
            logic [23:0] c24;
            logic signed [15:0] hi;
            logic signed [7:0]  lo;
            if (gv_wide) begin
              logic signed [63:0] c64;
              c24 = rne24w(acc_new, 6'd12);
              // canonical Q4.19 split (hi = nearest Q4.11 word, lo = signed
              // remainder); a raw c24[23:8]/c24[7:0] split reads back through
              // join24 as hi*256 + $signed(lo) and is biased by -256 codes
              c64 = {{40{c24[23]}}, c24};
              hi = canon_hi(c64);
              lo = canon_lo(c64);
            end else begin
              hi = rne16w(acc_new, 6'd20);
              lo = 8'sd0;
            end
            if (pv_col < 10'd64) begin
              gv_row[16*pv_col[5:0] +: 16] <= hi;
              gv_rowl[8*pv_col[5:0] +: 8] <= lo;
            end else begin
              gv_rowh[16*pv_col[5:0] +: 16] <= hi;
              gv_rowhl[8*pv_col[5:0] +: 8] <= lo;
            end
            gv_acc <= 56'sd0;
          end else begin
            gv_acc <= acc_new;
          end
          if (last_word) begin
            sub <= 6'd2;
          end else begin
            nc = gv_col; nk = gv_kb + 2'd1;
            if (nk == per_col(p_mat)) begin nk = 2'd0; nc = gv_col + 10'd1; end
            gv_col <= nc; gv_kb <= nk;
            pv_col <= gv_col; pv_kb <= gv_kb;
            // operand registers describe the word presented in this cycle;
            // they are consumed together with its data in the next cycle.
            vrf_ra <= (gv_kb == 2'd0) ? p_slo : p_shi;
            ob_off <= 7'((16'(gv_col) * 16'(MAT_K_V[p_mat*9 +: 9])) & 16'd63);
            sub <= 6'd1;
          end
        end
        6'd2: begin
          vrf_we <= 1'b1; vrf_wa <= p_dst;
          vrf_wd <= gv_row; vrf_lwd <= gv_rowl;
          sub <= (p_n > 10'd64) ? 6'd3 : 6'd4;
        end
        6'd3: begin
          vrf_we <= 1'b1; vrf_wa <= p_dhi;
          vrf_wd <= gv_rowh; vrf_lwd <= gv_rowhl;
          sub <= 6'd4;
        end
        6'd4: begin sub <= 6'd0; step <= ret_step; end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- LM head GEMV: 512 int32 logit codes into the logits macro plus the
    //      running argmax (smallest index on ties) ----
    X_GEMV2: begin
      // One output column per cycle after the single-cycle macro-read fill.
      // Cycle t presents the word/scale address of column c and consumes the
      // data of column c-1 (address presented in t-1); the logits write and
      // the argmax fold are registered in the same cycle.  K=128 matrices use
      // two consecutive cycles (two k-blocks) per column.
      case (sub)
        6'd0: begin
          gv_acc <= 56'sd0;
          gv_max <= 32'sh80000000; gv_argi <= 9'd0;
          pv_col <= 10'd0; pv_kb <= 2'd0;
          if (per_col(p_mat) > 2'd1) begin gv_col <= 10'd0; gv_kb <= 2'd1; end
          else begin gv_col <= 10'd1; gv_kb <= 2'd0; end
          vrf_ra <= p_slo;
          oa_src <= 3'd0; oa_w <= matw(p_mat); oa_off <= 7'd0;
          ob_src <= 3'd6; ob_w <= matw(p_mat); ob_off <= 7'd0;
          sub <= 6'd1;
        end
        6'd1: begin
          logic signed [15:0] scv;
          logic signed [55:0] pterm, acc_new;
          logic               last_word;
          logic [9:0]         nc;
          logic [1:0]         nk;
          scv = $signed(sc_rd[16*(16'(sc_slot(p_mat, pv_col, pv_kb)) % 16'd10) +: 16]);
          pterm = mac_dot_wide * scv;
          acc_new = (pv_kb == 2'd0) ? pterm : gv_acc + pterm;
          last_word = (pv_col + 10'd1 == p_n) && (pv_kb + 2'd1 == per_col(p_mat));
          if (pv_kb + 2'd1 == per_col(p_mat)) begin
            logic signed [31:0] code;
            code = rne32w(acc_new, 6'd20);
            lg_we <= 1'b1;
            lg_wa <= 7'(pv_col >> 2);
            lg_wstrb <= 16'h000F << (4*(pv_col[1:0]));
            lg_wd <= {{96{code[31]}}, code} << (32*(pv_col[1:0]));
            if (pv_col == 10'd0 || (code > gv_max)) begin
              gv_max <= code; gv_argi <= pv_col[8:0];
            end
            gv_acc <= 56'sd0;
          end else begin
            gv_acc <= acc_new;
          end
          if (last_word) begin
            sub <= 6'd2;
          end else begin
            nc = gv_col; nk = gv_kb + 2'd1;
            if (nk == per_col(p_mat)) begin nk = 2'd0; nc = gv_col + 10'd1; end
            gv_col <= nc; gv_kb <= nk;
            pv_col <= gv_col; pv_kb <= gv_kb;
            // operand registers describe the word presented in this cycle;
            // they are consumed together with its data in the next cycle.
            vrf_ra <= (gv_kb == 2'd0) ? p_slo : p_shi;
            ob_off <= 7'((16'(gv_col) * 16'(MAT_K_V[p_mat*9 +: 9])) & 16'd63);
            sub <= 6'd1;
          end
        end
        6'd2: begin sub <= 6'd0; step <= ret_step; end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- depthwise 4-tap causal convolution + SiLU of the pre-conv vector
    //      (p_src = current input) -> S_Q / S_K / S_V by p_stream ----
    X_CONV: begin
      case (sub)
        6'd0, 6'd2, 6'd4: begin
          cv_ra <= 4'(3*p_stream + 4'(hrow(hist_ptr[p_stream], 2'(sub[2:1]))));
          ck_ra <= 4'(4*p_stream + 4'(sub >> 1));
          oa_src <= 3'd4; oa_w <= 3'd3; oa_off <= 7'd0;
          ob_src <= 3'd5; ob_w <= 3'd3; ob_off <= 7'd0;
          mac_clr <= (sub == 6'd0); mac_wmode <= 1'b0;
          // kernel Q4.11 x pre-conv input Q4.11 = 2^22; shift 3 -> Q4.19
          mac_en <= 1'b0; mac_scl <= 24'd1; mac_sh <= 6'd3;
          ow_off <= 7'd0; ow_w <= 3'd3;
          sub <= sub + 6'd1;
        end
        6'd1, 6'd3, 6'd5: begin mac_en <= 1'b1; sub <= sub + 6'd1; end
        6'd6: begin                                   // tap 3: current input
          ck_ra <= 4'(4*p_stream + 4'd3);
          vrf_ra <= p_src;
          oa_src <= 3'd0; oa_w <= 3'd3; oa_off <= 7'd0;
          ob_src <= 3'd5; ob_w <= 3'd3; ob_off <= 7'd0;
          mac_en <= 1'b0;
          vrf_wa <= p_dst;
          sub <= 6'd7;
        end
        6'd7: begin mac_en <= 1'b1; sub <= 6'd8; end
        6'd8: begin                                   // wait for the tap-3 accumulate
          sub <= 6'd9;                                // (mac_en is registered, so the
        end                                           //  sum commits one cycle later)
        6'd9: begin
          // pre-activation -> p_dst; capture the pre-conv input from VRF port A
          // (still p_src in this cycle) into the oldest history row.  Tap 0 was
          // already read from that row, so overwriting it here is safe.  The
          // history must store the pre-conv *input*, not the conv result.
          logic [1023:0] pv;
          logic [511:0]  pl;
          for (int i = 0; i < 64; i++) begin
            pv[i*16 +: 16] = mac_acc_wout[i*24 + 8 +: 16];
            pl[i*8 +: 8]   = mac_acc_wout[i*24 +: 8];
          end
          vrf_we <= 1'b1; vrf_wd <= pv; vrf_lwd <= pl; vrf_wa <= p_dst;
          cv_we <= 1'b1;
          cv_wa <= 4'(3*p_stream + 4'(hrow(hist_ptr[p_stream], 2'd0)));
          cv_wd <= vrf_va;
          cv_wstrb <= {128{1'b1}};
          sub <= 6'd10;
        end
        // SiLU on the Q4.19 pre-activation (4 x 16 lanes) from p_dst to the
        // stream destination S_Q/S_K/S_V
        6'd10, 6'd12, 6'd14, 6'd16: begin
          vrf_ra <= p_dst;
          sub <= sub + 6'd1;
        end
        6'd11, 6'd13, 6'd15, 6'd17: begin
          logic [1023:0] yv;
          logic [511:0]  yl;
          logic [6:0]    goff;
          for (int i = 0; i < 16; i++) begin
            logic signed [23:0] x24;
            logic signed [23:0] s24;
            logic signed [63:0] c64;
            x24 = j24(vrf_va[(16*((sub - 6'd11) >> 1) + i)*16 +: 16],
                      vrf_lva[(16*((sub - 6'd11) >> 1) + i)*8 +: 8]);
            s24 = silu24(x24);
            // canonical Q4.19 split: hi = nearest Q4.11 word, lo = signed
            // remainder (a raw s24[23:8]/s24[7:0] split reads back as
            // hi*256 + $signed(lo) and would be biased by -256 codes)
            c64 = {{40{s24[23]}}, s24};
            yv[i*16 +: 16] = canon_hi(c64);
            yl[i*8 +: 8]   = canon_lo(c64);
          end
          goff = 7'(16*((sub - 6'd11) >> 1));
          vrf_we <= 1'b1;
          vrf_wa <= (p_stream == 4'd0) ? S_Q : (p_stream == 4'd1) ? S_K : S_V;
          vrf_wd <= mergev(vrf_vc, shl16({768'd0, yv}, goff), goff, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({384'd0, yl}, goff), goff, 7'd16);
          sub <= sub + 6'd1;
        end
        6'd18: begin
          // the history row was written at sub9; only the pointer advances
          sub <= 6'd19;
        end
        6'd19: begin
          hist_ptr[p_stream] <= (hist_ptr[p_stream] == 2'd2) ? 2'd0
                                                             : hist_ptr[p_stream] + 2'd1;
          sub <= 6'd0; step <= ret_step;
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- MoE top-2 selection: stable argmax over the 8 biased router scores
    //      in S_G with the pre-bias scores in S_M -> mz_sel0/1, mz_w0/1
    //      (Q2.13 route weights; 2.828/sum).  S_M is then cleared so the
    //      expert outputs can accumulate into it. ----
    X_TOP2: begin
      case (sub)
        6'd0: begin vrf_ra <= S_G; sub <= 6'd1; end
        6'd1: begin mz_sel0 <= argmax8w(vrf_va, vrf_lva, 3'd0, 1'b0); sub <= 6'd2; end
        6'd2: begin mz_sel1 <= argmax8w(vrf_va, vrf_lva, mz_sel0, 1'b1); vrf_ra <= S_M; sub <= 6'd3; end
        6'd3: begin
          mz_s0 <= j24(vrf_va[16*mz_sel0 +: 16], vrf_lva[8*mz_sel0 +: 8]);
          mz_s1 <= j24(vrf_va[16*mz_sel1 +: 16], vrf_lva[8*mz_sel1 +: 8]);
          sub <= 6'd4;
        end
        6'd4: begin
          logic [31:0] den;
          den = 32'($unsigned(mz_s0)) + 32'($unsigned(mz_s1));
          if (den == 32'd0) den = 32'd1;
          sfu_op <= 2'd1; sfu_din <= {16'd0, den}; sfu_sh <= 6'd30;
          sub <= 6'd5;
        end
        6'd5: begin
          sfu_r <= sfu_dout;
          sfu_op <= 2'd2;
          sfu_din <= 48'(48'($signed(mz_s0)) * 48'sd23167);
          sfu_din2 <= sfu_dout;
          sfu_sh <= 6'd30;
          sub <= 6'd6;
        end
        6'd6: begin
          mz_w0 <= rne16q($signed(sfu_dout), 6'd0);
          sfu_din <= 48'(48'($signed(mz_s1)) * 48'sd23167);
          sub <= 6'd7;
        end
        6'd7: begin
          mz_w1 <= rne16q($signed(sfu_dout), 6'd0);
          vrf_wa <= S_M;
          sub <= 6'd8;
        end
        6'd8: begin vrf_we <= 1'b1; vrf_wd <= 1024'd0; vrf_lwd <= 512'd0;
                     sub <= 6'd0; step <= ret_step; end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- accumulate one expert output: S_M += rne(p_src * p_sc >> 13) ----
    X_MOEACC: begin
      case (sub)
        6'd0: begin
          vrf_ra <= p_src; vrf_wa <= S_C;
          alu_op <= 3'd3; alu_sh <= 6'd13; alu_s <= p_sc; alu_wide <= 1'b1;
          alu_a_src <= 3'd0; alu_aoff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd1;
        end
        6'd1: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd2;
        end
        6'd2: begin
          vrf_ra <= p_src; vrf_wa <= S_C;
          alu_op <= 3'd3; alu_sh <= 6'd13; alu_s <= p_sc; alu_wide <= 1'b1;
          alu_aoff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd3;
        end
        6'd3: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd4;
        end
        6'd4: begin
          vrf_ra <= S_M; vrf_rb <= S_C; vrf_wa <= S_M;
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_s <= 16'sd0; alu_wide <= 1'b1;
          alu_aoff <= 7'd0; alu_boff <= 7'd0; alu_w <= 3'd2;
          sub <= 6'd5;
        end
        6'd5: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd0), 7'd0, 7'd48);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd0), 7'd0, 7'd48);
          sub <= 6'd6;
        end
        6'd6: begin
          vrf_ra <= S_M; vrf_rb <= S_C; vrf_wa <= S_M;
          alu_op <= 3'd0; alu_sh <= 6'd0; alu_wide <= 1'b1;
          alu_aoff <= 7'd48; alu_boff <= 7'd48; alu_w <= 3'd0;
          sub <= 6'd7;
        end
        6'd7: begin
          vrf_we <= 1'b1;
          vrf_wd <= mergev(vrf_vc, shl16({256'd0, alu_y}, 7'd48), 7'd48, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, shl8({128'd0, alu_y_lo}, 7'd48), 7'd48, 7'd16);
          sub <= 6'd0; step <= ret_step;
        end
        default: begin sub <= 6'd0; step <= ret_step; end
      endcase
    end

    // ---- Q5.18 router sigmoid on the 8 lanes of p_src -> p_dst ----
    X_SIG24: begin
      case (sub)
        6'd0: begin vrf_ra <= p_src; sub <= 6'd1; end
        default: begin
          logic [1023:0] yv;
          logic [511:0]  yl;
          for (int i = 0; i < 64; i++) begin
            if (i < 8) begin
              logic signed [23:0] x24;
              logic signed [18:0] s18;
              logic signed [63:0] c64;
              x24 = j24(vrf_va[i*16 +: 16], vrf_lva[i*8 +: 8]);
              s18 = sig24(x24);
              c64 = 64'(s18) << 1;               // Q5.18 -> Q4.19 code
              yv[i*16 +: 16] = canon_hi(c64);
              yl[i*8 +: 8] = canon_lo(c64);
            end else begin
              yv[i*16 +: 16] = 16'd0;
              yl[i*8 +: 8] = 8'd0;
            end
          end
          vrf_we <= 1'b1;
          vrf_wa <= p_dst;
          vrf_wd <= yv;
          vrf_lwd <= yl;
          sub <= 6'd0; step <= ret_step;
        end
      endcase
    end

    // ---- Q4.19 SiLU over 16-lane groups: p_dst = silu(p_src) ----
    X_SILU24: begin
      case (sub[0])
        1'b0: begin vrf_ra <= p_src; vrf_wa <= p_dst; sub <= sub + 6'd1; end
        default: begin
          logic [255:0]  yv;
          logic [127:0]  yl;
          logic [1023:0] full_yv;
          logic [511:0]  full_yl;
          logic [6:0]    goff;
          for (int i = 0; i < 16; i++) begin
            logic signed [23:0] x24;
            logic signed [23:0] s24;
            logic signed [63:0] c64;
            x24 = j24(vrf_va[(16*(sub>>1)+i)*16 +: 16],
                      vrf_lva[(16*(sub>>1)+i)*8 +: 8]);
            s24 = silu24(x24);
            c64 = {{40{s24[23]}}, s24};
            yv[i*16 +: 16] = canon_hi(c64);
            yl[i*8 +: 8] = canon_lo(c64);
          end
          goff = 7'(16*(sub >> 1));
          full_yv = shl16({768'd0, yv}, goff);
          full_yl = shl8({384'd0, yl}, goff);
          vrf_we <= 1'b1; vrf_wa <= p_dst;
          vrf_wd <= mergev(vrf_vc, full_yv, goff, 7'd16);
          vrf_lwd <= mergev8(vrf_lvc, full_yl, goff, 7'd16);
          if (9'((sub >> 1) + 6'd1) * 9'd16 >= p_len) begin
            sub <= 6'd0; step <= ret_step;
          end else sub <= sub + 6'd1;
        end
      endcase
    end

    // ---- executors not referenced by the program graph: explicit pass-through
    //      (they own no state and are kept for the step encoding) ----
    X_GEMVCONV, X_RED, X_SFU, X_MACD, X_MACC: begin
      sub <= 6'd0; step <= ret_step;
    end
