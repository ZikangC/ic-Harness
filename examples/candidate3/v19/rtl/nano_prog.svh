// ---------------------------------------------------------------------------
// Token program + loader + memory phases for nano_core (textually included in
// nano_core's sequential process).  All control values are registered, so an
// operation is scheduled as: set operands/controls (sub 0) -> capture the
// result (sub 1) -> continue.  Ordering follows reference.py step().
// ---------------------------------------------------------------------------

  case (phase)
    // -----------------------------------------------------------------------
    P_IDLE: begin
      busy <= 1'b0;
      if (cmd_valid) begin
        if (cmd_data == RUN_W) begin
          seq_len <= {1'b0, cfg_seq_m8} + 7'd8;
          phase <= P_PRE; busy <= 1'b1; cycles <= 32'd0;
          ld_mat <= 7'd0; ld_total <= 32'd0;
        end else begin
          case (cmd_data[31:24])
            8'h01: begin cfg_seq_m8 <= cmd_data[5:0]; cfg_grp64 <= cmd_data[6]; end
            8'h02: cfg_wbase[27:4] <= cmd_data[23:0];
            8'h03: cfg_wbase[35:28] <= cmd_data[7:0];
            8'h04: cfg_tbase[27:4] <= cmd_data[23:0];
            8'h05: cfg_tbase[35:28] <= cmd_data[7:0];
            8'h06: cfg_obase[27:4] <= cmd_data[23:0];
            8'h07: cfg_obase[35:28] <= cmd_data[7:0];
            default: ;
          endcase
        end
      end
    end

    // -----------------------------------------------------------------------
    P_PRE: begin
      if (ld_mat == 7'(NMAT)) begin
        // small parameters: sum(2*SM_LEN) = 1970 int16 words; then the
        // convolution tail section (CONV_SECTION_BYTES).
        ld_total <= ld_total + 32'(SMALL_SECTION_BYTES) + 32'(CONV_SECTION_BYTES);
        ld_nb <= 16'((ld_total + 32'(SMALL_SECTION_BYTES) + 32'(CONV_SECTION_BYTES)
                      + 32'd15) >> 4);
        ld_mat <= 7'd0; ld_st <= 3'd0; ld_bidx <= 4'd0; ld_out <= 32'd0;
        ld_bytecnt <= 32'd0; ld_beat_valid <= 1'b0; ld_req <= 13'd0;
        // deterministic loader cursors: no dependence on a previous RUN
        ld_cnt <= 10'd0; ld_n <= 10'd0; ld_t <= 9'd0; ld_r <= 8'd0;
        ld_lane <= 7'd0; ld_stage <= 320'd0; ld_sph <= 4'd0; ld_smph <= 2'd0;
        ld_smid <= 6'd0; ld_smcnt <= 10'd0; ld_qzw <= mqzb;
        phase <= P_LOAD;
      end else begin
        ld_total <= ld_total + m_bytes;
        ld_mat <= ld_mat + 7'd1;
      end
    end

    // -----------------------------------------------------------------------
    P_LOAD: begin
      // ---- beat request: combinational strobe (see rd_issue) ----
      if (rd_issue && rd_ready) ld_req <= ld_req + 13'd1;
      // ---- byte window: consume ld_c bytes of ld_src starting at ld_off ----
      // The FIFO head is read combinationally in nano_mem, so a fresh beat is
      // consumed in the cycle it is popped (fifo_pop is combinational as well).
      // The outstanding-beat counter is updated by ONE expression: an accepted
      // request and a beat pop can happen in the same cycle, and two
      // nonblocking assignments to ld_out would drop the increment.
      ld_out <= ld_out
              + 32'((rd_issue && rd_ready) ? 1 : 0)
              - 32'(((ld_c != 5'd0) && !ld_beat_valid && fifo_valid &&
                     ((ld_st != 3'd5) || ld_more)) ? 1 : 0);
      if (ld_c != 5'd0) begin
        if (ld_beat_valid) begin
          if (ld_off + ld_c >= 5'd16) ld_beat_valid <= 1'b0;
          else                        ld_bidx <= ld_bidx + 4'(ld_c);
        end else begin
          if (ld_c < 5'd16) begin
            ld_beat <= fifo_data; ld_bidx <= 4'(ld_c); ld_beat_valid <= 1'b1;
          end
        end
      end
      // ---- body ----
      case (ld_st)
        // ---- per-matrix blocks: z, s, q ----
        3'd0: begin                                     // z block: R*N bytes
          if (ld_c != 5'd0) begin
            for (int i = 0; i < 16; i++)
              if (i < int'(ld_c))
                ld_zbuf[ld_cnt[8:0] + 9'(i)] <= ldbyte(ld_src, ld_off, i);
            ld_cnt <= ld_cnt + 10'(ld_c);
            if (ld_cnt + 10'(ld_c) == 10'(mR) * 10'(mN)) begin
              ld_cnt <= 10'd0; ld_st <= 3'd1; ld_r <= 8'd0; ld_n <= 10'd0;
            end
          end
        end
        3'd1: begin                                     // s block: 2*R*N bytes
          if (ld_c != 5'd0) begin
            logic [159:0] swd;
            logic [19:0]  swb;
            int sl0, fit, n2, r2, slot;
            sl0 = int'(mscb) + int'(ld_n) * int'(mper) + int'(ld_r);
            fit = int'(ld_c) / 2;
            swd = 160'd0; swb = 20'd0;
            for (int i = 0; i < 8; i++) begin
              if (i < fit) begin
                slot = (sl0 + i * int'(mper)) % 10;
                swd[16*slot +: 16] = {ldbyte(ld_src, ld_off, 2*i+1),
                                      ldbyte(ld_src, ld_off, 2*i)};
                swb[2*slot +: 2] = 2'b11;
              end
            end
            sc_we <= 1'b1;
            sc_wa <= 9'(sl0 / 10);
            sc_wd <= swd;
            sc_wstrb <= swb;
            n2 = int'(ld_n); r2 = int'(ld_r);
            for (int i = 0; i < 8; i++)
              if (i < fit) begin
                n2 = n2 + 1;
                if (n2 == int'(mN)) begin n2 = 0; r2 = r2 + 1; end
              end
            if (r2 >= int'(mR)) begin
              ld_r <= 8'd0; ld_cnt <= 10'd0; ld_st <= 3'd2;
              ld_n <= 10'd0; ld_t <= 9'd0; ld_lane <= 7'd0;
              ld_stage <= 320'd0; ld_qzw <= mqzb;
            end else begin
              ld_n <= 10'(n2); ld_r <= 8'(r2);
            end
          end
        end
        3'd2: begin                                     // q block: K*N/2 bytes
          if (ld_c != 5'd0) begin
            logic [7:0] zrow;
            logic [7:0] zv;
            logic [319:0] seg;
            logic [319:0] ld_new;
            int np, t2, n2;
            logic [7:0] bq;
            zrow = (mK == 9'd128 && cfg_grp64 && ld_t >= 9'd32) ? 8'd1 : 8'd0;
            zv = ld_zbuf[(16'(zrow) * 16'(mN) + 16'(ld_n)) & 9'h1FF];
            np = int'(ld_c);                         // one byte = one lane pair
            seg = 320'd0;
            for (int i = 0; i < 32; i++) begin
              if (i < np) begin
                bq = ldbyte(ld_src, ld_off, i);
                seg[i*10 +: 5]   = 5'(16'(bq[3:0]) - 16'(zv));
                seg[i*10+5 +: 5] = 5'(16'(bq[7:4]) - 16'(zv));
              end
            end
            ld_new = ld_stage | (seg << (5*int'(ld_lane)));
            if (ld_lane + 7'(2*np) >= 7'd64) begin
              qz_we <= 1'b1; qz_wa <= ld_qzw; qz_wd <= ld_new;
              ld_qzw <= ld_qzw + 13'd1; ld_lane <= 7'd0; ld_stage <= 320'd0;
            end else begin
              ld_lane <= ld_lane + 7'(2*np); ld_stage <= ld_new;
            end
            t2 = int'(ld_t) + np;
            n2 = int'(ld_n);
            if (t2 >= int'(mK) / 2) begin
              t2 = 0;
              n2 = n2 + 1;
            end
            if (n2 >= int'(mN)) begin
              ld_st <= 3'd0; ld_cnt <= 10'd0; ld_t <= 9'd0;
              if (ld_mat + 7'd1 == 7'(NMAT)) begin
                ld_st <= 3'd3; ld_smid <= 6'd0; ld_smcnt <= 10'd0;
                ld_smph <= 2'd0; ld_smoff9 <= 13'd0;
              end else ld_mat <= ld_mat + 7'd1;
            end else begin
              ld_t <= 9'(t2); ld_n <= 10'(n2);
            end
          end
        end
        // ---- small parameters: raw int16 stream, each tensor in stream order,
        //      value i of tensor s lives at a_m_small slot SM_OFF[s]+i ----
        3'd3: begin
          if (ld_c != 5'd0) begin
            logic [255:0] swd;
            logic [31:0]  swb;
            int sl0, lane, fit, cnt2;
            logic [15:0] rawv, decv;
            sl0  = int'(SM_OFF_V[ld_smid*16 +: 16]) + int'(ld_smcnt);
            lane = sl0 % 16;
            fit  = int'(ld_c) / 2;
            swd = 256'd0; swb = 32'd0;
            for (int i = 0; i < 8; i++) begin
              if (i < fit) begin
                rawv = {ldbyte(ld_src, ld_off, 2*i+1), ldbyte(ld_src, ld_off, 2*i)};
                decv = (SM_KIND_V[ld_smid*2 +: 2] == 2'd0)
                     ? 16'(16'sd16384 + 16'sd2 * $signed(rawv)) : rawv;
                swd[16*(lane+i) +: 16] = decv;
                swb[2*(lane+i) +: 2] = 2'b11;
              end
            end
            sm_we <= 1'b1;
            sm_wa <= 8'(sl0 >> 4);
            sm_wd <= swd;
            sm_wstrb <= swb;
            cnt2 = int'(ld_smcnt) + fit;
            if (cnt2 >= int'(SM_LEN_V[ld_smid*12 +: 12])) begin
              ld_smcnt <= 10'd0;
              if (ld_smid + 6'd1 == 6'(NSMALL)) begin
                ld_st <= 3'd4;                         // convolution tail section
                ld_smph <= 2'd0;
              end else ld_smid <= ld_smid + 6'd1;
            end else ld_smcnt <= 10'(cnt2);
          end
        end
        // ---- convolution kernel tail: CONV_VALUES raw int16, tap-major,
        //      word = 4*tensor + tap, lane = channel ----
        3'd4: begin
          if (ld_c != 5'd0) begin
            logic [1023:0] cwd;
            logic [127:0]  cwb;
            int ci, lane, fit;
            ci = int'(ld_smcnt);
            lane = ci % 64;
            fit = int'(ld_c) / 2;
            cwd = 1024'd0; cwb = 128'd0;
            for (int i = 0; i < 8; i++) begin
              if (i < fit) begin
                cwd[16*(lane+i) +: 16] = {ldbyte(ld_src, ld_off, 2*i+1),
                                          ldbyte(ld_src, ld_off, 2*i)};
                cwb[2*(lane+i) +: 2] = 2'b11;
              end
            end
            ck_we <= 1'b1;
            ck_wa <= 4'(ci >> 6);
            ck_wd <= cwd;
            ck_wstrb <= cwb;
            if (ci + fit >= int'(CONV_VALUES)) ld_st <= 3'd5;   // finished
            else ld_smcnt <= 10'(ci + fit);
          end
        end
        default: begin                                    // drain beat padding
          if (!ld_more) begin
            phase <= P_TOKRD; sub <= 6'd0; cnt <= '0;
          end
        end
      endcase
    end

    // -----------------------------------------------------------------------
    P_TOKRD: begin
      // One read beat carries four uint32 little-endian token ids, so the token
      // phase is a beat-level stream: request a beat, capture its four ids in
      // the cycle it arrives, and stop after ceil(seq_len/4) beats.
      if (sub == 6'd0) begin
        sub <= 6'd1; ld_req <= 16'd0; cnt <= '0;
        ld_nb <= 16'(({6'b0, seq_len} * 4 + 15) >> 4);
      end else if (sub == 6'd1) begin
        if (rd_issue && rd_ready) ld_req <= ld_req + 13'd1;
        if (fifo_valid && (cnt < ld_nb[6:0])) begin
          for (int k = 0; k < 4; k++)
            tok_buf[{cnt, 2'b0} + 2'(k)] <= fifo_data[32*k +: 32];
          cnt <= cnt + 7'd1;
          if (cnt + 7'd1 == ld_nb[6:0]) sub <= 6'd2;
        end
      end else begin
        phase <= P_INIT; sub <= 6'd0; init_cnt <= 8'd0;
      end
    end

    // -----------------------------------------------------------------------
    P_INIT: begin
      if (init_cnt < 8'd64) begin
        st_we <= 1'b1; st_wa <= init_cnt[5:0]; st_wd <= 768'd0;
        init_cnt <= init_cnt + 8'd1;
      end else if (init_cnt < 8'd76) begin
        // Zero the three conv-history rows of every stream.  The write strobe
        // must be driven here: a_m_conv has no reset value, so omitting it
        // leaves the previous RUN's history in place and also makes the first
        // RUN depend on the SRAM power-on state (spec §4, Appendix D).
        cv_we <= 1'b1; cv_wa <= (init_cnt - 8'd64);
        cv_wd <= 1024'd0; cv_wstrb <= {128{1'b1}};
        init_cnt <= init_cnt + 8'd1;
      end else begin
        for (int i = 0; i < 4; i++) cv_base[i] <= 3'd0;
        for (int i = 0; i < 3; i++) hist_ptr[i] <= 2'd0;
        cpos <= 7'd0; at_pos <= 8'd0;
        phase <= P_STEP; step <= E0_EMB; sub <= 6'd0; tok <= 7'd0;
      end
    end

    // -----------------------------------------------------------------------
    P_STEP: begin
      `include "nano_token.svh"
    end

    // -----------------------------------------------------------------------
    P_DRAIN: begin
      // 2064 bytes per position (128 logit words + one argmax beat, 16-byte
      // aligned; spec section 3).  The macro read has one cycle of latency and
      // the drain accepts one beat per cycle, so the read address is held one
      // beat ahead of `drain_beat` (precharged to word 1 by E2_END, word 0 is
      // on the port in the first beat).  The address advances only when the
      // beat is accepted, so request backpressure cannot make it run ahead of
      // the beat counter and skip words.
      if (wr_issue && wr_ready) begin
        if (drain_beat == 16'd128) begin
          drain_beat <= 16'd0;
          if (tok + 7'd1 == seq_len) begin
            phase <= P_DONE; rsp_hold <= 1'b1;
          end else begin
            tok <= tok + 7'd1; phase <= P_STEP; step <= E0_EMB; sub <= 6'd0;
          end
        end else begin
          // keep the logits read address one beat ahead of the beat counter
          // (macro read latency 1 cycle, one posted-write beat per cycle)
          lg_ra <= (drain_beat < 16'd126) ? 7'(drain_beat + 16'd2) : 7'd127;
          drain_beat <= drain_beat + 16'd1;
        end
      end
    end

    // -----------------------------------------------------------------------
    P_DONE: begin
      if (rsp_hold && rsp_ready) begin
        rsp_hold <= 1'b0; phase <= P_IDLE; busy <= 1'b0;
      end
    end
    default: phase <= P_IDLE;
  endcase
