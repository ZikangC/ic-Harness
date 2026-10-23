// ---------------------------------------------------------------------------
// Nano Decoder accelerator - leaf datapath and storage primitives.
// Contract (PLAN.md section 2):
//   * activations / K/V are signed Q4.11 in 16 bits; the KDA state is a
//     24-bit Q4.19 split (hi*256 + lo) held in one 768-bit macro word; every
//     writeback rounds to nearest-even and saturates.
//   * weight operands are signed 5-bit (q-z) with a raw int16 Q3.12 group
//     scale; the shared 2^-12 tail is applied once per (column, group).
//   * wide accumulators hold products of two fixed-point values; they are
//     rescaled by (scl, sh) with a single nearest-even rounding.
// The nonlinear helpers implement exactly the algorithms verified in
// tools/gen_lut.py (bit-accurate oracles used by tb_leaf).
// ---------------------------------------------------------------------------
`include "nano_lut.svh"

// 1R1W macro with synchronous read; abstract storage primitive of the
// platform (blackboxed in the synthesis flow, charged as macro area).
module sram_1r1w #(
  parameter int DEPTH = 4096,
  parameter int WIDTH = 320
) (
  input  logic                     clk,
  input  logic                     rd_en,
  input  logic [$clog2(DEPTH)-1:0] rd_addr,
  output logic [WIDTH-1:0]         rd_data,
  input  logic                     we,
  input  logic [$clog2(DEPTH)-1:0] wr_addr,
  input  logic [WIDTH-1:0]         wr_data,
  input  logic [WIDTH/8-1:0]       wr_strb
);
  logic [WIDTH-1:0] mem [DEPTH];

  always_ff @(posedge clk) begin
    if (rd_en) rd_data <= mem[rd_addr];
    if (we) begin
      for (int b = 0; b < WIDTH/8; b++)
        if (wr_strb[b]) mem[wr_addr][8*b +: 8] <= wr_data[8*b +: 8];
    end
  end
endmodule

// ---------------------------------------------------------------------------
// Vector register file: NV vectors of DW signed 16-bit lanes plus an 8-bit
// low-resolution plane.  The architectural value of a lane is
//     value = hi * 2^-11 + lo * 2^-19    (Q4.11 high word + signed extension)
// so a Q4.19 code is (hi*256 + lo).  Narrow writers set lo to zero; every
// write supplies the complete low vector so unwritten lanes keep their value.
module vrf #(
  parameter int NV = 16,
  parameter int DW = 64,
  parameter int AI = 4
) (
  input  logic               clk,
  input  logic [AI-1:0]      ra,
  input  logic [AI-1:0]      rb,
  input  logic [AI-1:0]      rc,
  input  logic [AI-1:0]      rd,
  output logic [DW*16-1:0]   va,
  output logic [DW*16-1:0]   vb,
  output logic [DW*16-1:0]   vc,
  output logic [DW*16-1:0]   vd,
  output logic [DW*8-1:0]    la,
  output logic [DW*8-1:0]    lb,
  output logic [DW*8-1:0]    lc,
  output logic [DW*8-1:0]    ld,
  input  logic               we,
  input  logic [AI-1:0]      wa,
  input  logic [DW*16-1:0]   wd,
  input  logic [DW*8-1:0]    wd_lo
);
  logic [DW*16-1:0] mem [NV];
  logic [DW*8-1:0]  lmem [NV];
  assign va = mem[ra];
  assign vb = mem[rb];
  assign vc = mem[rc];
  assign vd = mem[rd];
  assign la = lmem[ra];
  assign lb = lmem[rb];
  assign lc = lmem[rc];
  assign ld = lmem[rd];
  always_ff @(posedge clk) if (we) begin
    mem[wa]  <= wd;
    lmem[wa] <= wd_lo;
  end
endmodule

// ---------------------------------------------------------------------------
// Canonical split of a Q4.19 code: hi is the nearest Q4.11 word and lo is the
// signed 8-bit remainder, so a Q4.19 value is exactly hi*256 + lo.
function automatic logic [23:0] canon24f (input logic signed [63:0] v);
  logic signed [63:0] q, rem, r;
  logic               up;
  if (v > 64'sd8388607) begin
    canon24f = 24'h7FFF7F;                    // +32767.996: saturated
  end else if (v < -64'sd8388608) begin
    canon24f = 24'h800000;                    // -32768.0: saturated
  end else begin
    q   = v >>> 8;
    rem = v & 64'sd255;
    up  = (rem > 64'sd128) | ((rem == 64'sd128) && q[0]);
    q   = q + (up ? 64'sd1 : 64'sd0);
    r   = v - (q << 8);
    // the signed remainder must stay in [-128,127]; a +128 tie is represented
    // by the next high word with remainder -128
    if (r > 64'sd127) begin q = q + 64'sd1; r = r - 64'sd256; end
    if (q > 64'sd32767) begin
      canon24f = 24'h7FFF7F;
    end else begin
      canon24f = {q[15:0], r[7:0]};
    end
  end
endfunction
function automatic logic signed [15:0] canon_hi (input logic signed [63:0] v);
  logic [23:0] c;
  c = canon24f(v);
  canon_hi = $signed(c[23:8]);
endfunction
function automatic logic signed [7:0] canon_lo (input logic signed [63:0] v);
  logic [23:0] c;
  c = canon24f(v);
  canon_lo = $signed(c[7:0]);
endfunction
// Reconstruction of the Q4.19 split value: the high word is the nearest Q4.11
// word and the low byte is the signed remainder, so the value is
// hi*256 + $signed(lo).  A plain {hi, lo} concatenation would zero-extend the
// byte and add 2^-11 whenever the remainder is negative.
function automatic logic signed [23:0] join24 (input logic [15:0] hi,
                                               input logic [7:0]  lo);
  join24 = $signed({hi, 8'd0}) + $signed({{16{lo[7]}}, lo});
endfunction

// ---------------------------------------------------------------------------
// exp / sigmoid / silu helpers, bit-exact with tools/gen_lut.py.
function automatic logic signed [47:0] exp_core (input logic signed [15:0] xin,
                                                 input logic [4:0] oshf);
  logic signed [15:0] x;
  logic signed [47:0] z;
  logic signed [47:0] ipart;
  logic [16:0] frac;
  logic [4:0]  m;
  logic [11:0] e;
  logic [47:0] poly;
  logic [47:0] P;
  logic signed [6:0] sh;
  logic [47:0] base;
  logic [47:0] rem;
  logic [47:0] half;
  logic        up;
  x = (xin > 16'sd0) ? 16'sd0 : xin;
  z = ($signed(x) * 48'sd94546) >>> 10;
  ipart = z >>> 17;
  frac  = z[16:0];
  m = frac[16:12];
  e = frac[11:0];
  poly = 48'd65536 + ((e * 48'd22713) >> 16) + (((e * e) * 48'd15743) >> 34);
  P = {31'b0, ETAB_V[m*17 +: 17]} * poly;
  sh = 7'sd32 - $signed({2'b0, oshf}) - ipart[6:0];
  if ((ipart < -48'sd40) || (sh > 7'sd62)) begin
    base = 48'd0; up = 1'b0;
  end else if (sh <= 0) begin
    base = P << (-sh); up = 1'b0;
  end else begin
    base = P >> sh;
    rem  = P & ((48'd1 << sh) - 48'd1);
    half = 48'd1 << (sh - 7'd1);
    up   = (rem > half) | ((rem == half) & base[0]);
  end
  exp_core = base + (up ? 48'd1 : 48'd0);
endfunction

// rne(2^k / X) for 1 <= X <= 2^k (X = 0 returns the saturated maximum).
function automatic logic [31:0] recip_f (input logic [47:0] X,
                                         input logic [5:0]  k);
  logic [5:0]  L;
  logic signed [6:0] s;
  logic [47:0] N;
  logic [16:0] m;
  logic [7:0]  idxw;
  logic [5:0]  idx;
  logic [16:0] r;
  logic [32:0] t;
  logic signed [17:0] u;
  logic [47:0] R;
  logic [47:0] base;
  logic [47:0] rem;
  logic [47:0] half;
  logic        up;
  logic [35:0] rt;
  if (X == 48'd0) begin
    recip_f = 32'hFFFFFFFF;
  end else begin
    L = 6'd0;
    for (int i = 0; i < 48; i++) if (X[i]) L = i[5:0];
    s = $signed({1'b0, k}) - $signed({1'b0, L});
    N = (s >= 0) ? (X << s) : (X >> (-s));
    if (N < (48'd1 << k)) N = 48'd1 << k;
    m = N >> (k - 6'd15);
    idxw = (N >> (k - 6'd6)) - 8'd64;
    idx = idxw[5:0];
    r = RCTAB_V[idx*16 +: 16];
    for (int it = 0; it < 2; it++) begin
      t = (m * r) >> 15;
      u = 18'sd65536 - $signed({1'b0, t[16:0]});
      rt = {1'b0, r} * u;
      r = (rt >> 15) & 17'h1FFFF;
    end
    up = 1'b0;
    if (s >= 7'sd15) begin
      R = {32'b0, r} << (s - 7'sd15);
    end else if (15 - s > 0) begin
      base = {32'b0, r} >> (15 - s);
      rem  = {32'b0, r} & ((48'd1 << (15 - s)) - 48'd1);
      half = 48'd1 << (15 - s - 7'd1);
      up   = (rem > half) | ((rem == half) & base[0]);
      R = base + (up ? 48'd1 : 48'd0);
    end else begin
      R = {32'b0, r};
    end
    recip_f = (R > 48'd4294967295) ? 32'hFFFFFFFF : R[31:0];
  end
endfunction

// rne(2^25 / sqrt(M)) for M >= 1 (Q1.14 result, clamped below 2^31).
function automatic logic [47:0] rsqrt_f (input logic [47:0] din);
  logic [5:0]  L;
  logic signed [6:0] d;
  logic signed [6:0] se;
  logic [47:0] N;
  logic [7:0]  idx;
  logic [13:0] y;
  logic [63:0] t;
  logic signed [33:0] u;
  logic signed [6:0] shr;
  logic [47:0] G;
  logic [47:0] base;
  logic [47:0] rem;
  logic [47:0] half;
  logic        up;
  logic [63:0] yt;
  L = 6'd0;
  for (int i = 0; i < 48; i++) if (din[i]) L = i[5:0];
  d  = 7'sd20 - $signed({1'b0, L});
  se = d + {6'b0, d[0]};
  N = (se >= 0) ? (din << se) : (din >> (-se));
  if (N < (48'd1 << 20)) N = 48'd1 << 20;
  idx = N[25:14] - 8'd64;
  y = RSTAB_V[idx*14 +: 14];
  for (int it = 0; it < 2; it++) begin
    t = ({16'b0, N} * {50'b0, y} * {50'b0, y}) >> 16;
    u = 34'sd3221225472 - $signed({2'b0, t[31:0]});    // 3<<30
    yt = {4'b0, y} * u;
    y = (yt >> 31) & 14'h3FFF;
  end
  shr = 7'sd2 + (se >>> 1);
  if (shr >= 0) begin
    G = {34'b0, y} << shr;
  end else begin
    base = {34'b0, y} >> (-shr);
    rem  = {34'b0, y} & ((48'd1 << (-shr)) - 48'd1);
    half = 48'd1 << (-shr - 7'd1);
    up   = (rem > half) | ((rem == half) & base[0]);
    G = base + (up ? 48'd1 : 48'd0);
  end
  rsqrt_f = (G > 48'sd2147483647) ? 48'd2147483647 : G;
endfunction

// ---------------------------------------------------------------------------
// Q5.18 sigmoid of a Q4.19 input (24-bit code).  exp_core only accepts
// non-positive arguments, so |x| is used and the result is selected by sign.
// The 8-bit low extension is re-inserted as a first-order exponential
// correction, which keeps the router comparison meaningful at a 2^-19 input
// resolution.  Output: 2^18 * sigmoid(x), clamped to [0, 2^18-1].
function automatic logic signed [18:0] sig24 (input logic signed [23:0] x);
  logic [23:0] ax;
  logic [15:0] ah;
  logic [7:0]  al;
  logic [47:0] ev, e, den, num, R, sg;
  if (x == 24'sh800000) ax = 24'h7FFFFF;
  else                  ax = x[23] ? 24'(0 - x) : x;
  ah  = 16'(ax >>> 8);
  al  = ax[7:0];
  ev  = exp_core($signed(16'(0 - ah)), 5'd18);   // exp(-|x|): exp_core takes x<=0
  e   = (ev * (48'd524288 - 48'(al))) >> 19;   // exp(-(ah*2^-11 + al*2^-19))
  den = 48'd262144 + e;
  R   = {16'd0, recip_f(den, 6'd36)};
  num = x[23] ? e : 48'd262144;
  sg  = (num * R) >> 18;
  if (sg > 48'd262143) sg = 48'd262143;
  sig24 = sg[18:0];
endfunction

// SiLU on a Q4.19 input: rne(x * sigmoid(x)) in the same Q4.19 code.
function automatic logic signed [23:0] silu24 (input logic signed [23:0] x);
  logic signed [18:0] sg;
  logic signed [63:0] p, q, rem, half;
  logic               up;
  sg = sig24(x);
  p  = $signed({{40{x[23]}}, x}) * $signed({{45{sg[18]}}, sg});
  q  = p >>> 18;
  rem  = p & ((64'sd1 << 18) - 64'sd1);
  half = 64'sd1 << 17;
  up   = (rem > half) | ((rem == half) & q[0]);
  q  = q + (up ? 64'sd1 : 64'sd0);
  if (q > 64'sd8388607) silu24 = 24'sh7FFFFF;
  else if (q < -64'sd8388608) silu24 = 24'sh800000;
  else silu24 = q[23:0];
endfunction

// ---------------------------------------------------------------------------
// MAC array: 64 signed 16x16 lanes with 40-bit per-lane accumulators.
module mac64 (
  input  logic               clk,
  input  logic               clr,
  input  logic               en,
  input  logic [1023:0]      opa,
  input  logic [1023:0]      opb,
  input  logic [64*8-1:0]    opa_lo,
  input  logic               wmode,
  input  logic [23:0]        scl,
  input  logic [5:0]         sh,
  output logic signed [47:0] dot_full,
  output logic signed [47:0] dot_lo,
  output logic signed [55:0] dot_wide,
  output logic [64*56-1:0]   acc,
  output logic [1023:0]      acc_out,
  output logic [64*24-1:0]   acc_wout
);
  logic signed [39:0] prod [64];
  logic signed [39:0] prod_w [64];
  logic signed [55:0] w0 [64];
  logic signed [55:0] w1 [32];
  logic signed [55:0] w2 [16];
  logic signed [55:0] w3 [8];
  logic signed [55:0] w4 [4];
  logic signed [55:0] w5 [2];
  logic signed [47:0] t0 [64];
  logic signed [47:0] t1 [32];
  logic signed [47:0] t2 [16];
  logic signed [47:0] t3 [8];
  logic signed [47:0] t4 [4];
  logic signed [47:0] t5 [2];
  logic signed [47:0] u1 [8];
  logic signed [47:0] u2 [4];
  logic signed [47:0] u3 [2];
  logic signed [55:0] acc_r [64];

  genvar gi;
  for (gi = 0; gi < 64; gi++) begin : g_prod
    assign prod[gi] = $signed(opa[gi*16 +: 16]) * $signed(opb[gi*16 +: 16]);
    // 24-bit (hi*256 + lo) x 16-bit product for the Q4.19 datapath
    assign prod_w[gi] = join24(opa[gi*16 +: 16], opa_lo[gi*8 +: 8])
                        * $signed(opb[gi*16 +: 16]);
  end

  always_comb begin
    for (int i = 0; i < 64; i++) t0[i] = prod[i];
    for (int i = 0; i < 32; i++) t1[i] = t0[2*i] + t0[2*i+1];
    for (int i = 0; i < 16; i++) t2[i] = t1[2*i] + t1[2*i+1];
    for (int i = 0; i < 8; i++)  t3[i] = t2[2*i] + t2[2*i+1];
    for (int i = 0; i < 4; i++)  t4[i] = t3[2*i] + t3[2*i+1];
    for (int i = 0; i < 2; i++)  t5[i] = t4[2*i] + t4[2*i+1];
    dot_full = t5[0] + t5[1];
    // lanes 0..31 (the low half used by the two-column 320-bit word layout)
    for (int i = 0; i < 8; i++)  u1[i] = t2[i];
    for (int i = 0; i < 4; i++)  u2[i] = u1[2*i] + u1[2*i+1];
    for (int i = 0; i < 2; i++)  u3[i] = u2[2*i] + u2[2*i+1];
    dot_lo = u3[0] + u3[1];
    // wide Q4.19 dot: sum of (hi*256 + lo) * opb over 64 lanes
    for (int i = 0; i < 64; i++) w0[i] = prod_w[i];
    for (int i = 0; i < 32; i++) w1[i] = w0[2*i] + w0[2*i+1];
    for (int i = 0; i < 16; i++) w2[i] = w1[2*i] + w1[2*i+1];
    for (int i = 0; i < 8; i++)  w3[i] = w2[2*i] + w2[2*i+1];
    for (int i = 0; i < 4; i++)  w4[i] = w3[2*i] + w3[2*i+1];
    for (int i = 0; i < 2; i++)  w5[i] = w4[2*i] + w4[2*i+1];
    dot_wide = w5[0] + w5[1];
  end

  always_ff @(posedge clk) begin
    for (int i = 0; i < 64; i++) begin
      if (clr)     acc_r[i] <= 56'sd0;
      else if (en) acc_r[i] <= acc_r[i] + (wmode
                    ? $signed({{16{prod_w[i][39]}}, prod_w[i]})
                    : $signed({{16{prod[i][39]}}, prod[i]}));
    end
  end

  for (gi = 0; gi < 64; gi++) assign acc[gi*56 +: 56] = acc_r[gi];

  always_comb begin
    for (int i = 0; i < 64; i++) begin
      logic signed [79:0] p;
      logic signed [79:0] q;
      logic signed [79:0] rem;
      logic signed [79:0] half;
      logic               up;
      p = acc_r[i] * $signed({1'b0, scl});
      q = 80'sd0;
      rem = 80'sd0;
      half = 80'sd0;
      up = 1'b0;
      if (sh == 0) begin
        q = p;
      end else begin
        q    = p >>> sh;
        rem  = p & ((80'sd1 << sh) - 80'sd1);
        half = 80'sd1 << (sh - 6'd1);
        up   = (rem > half) | ((rem == half) & q[0]);
        q    = q + (up ? 80'sd1 : 80'sd0);
      end
      if (q > 64'sd32767)       acc_out[i*16 +: 16] = 16'sh7FFF;
      else if (q < -64'sd32768) acc_out[i*16 +: 16] = 16'sh8000;
      else                      acc_out[i*16 +: 16] = q[15:0];
      // The wide result is consumed as a Q4.19 *split* value (hi plane =
      // bits [23:8], signed low plane = bits [7:0]) and reconstructed as
      // hi*256 + $signed(lo).  The split must therefore be canonical: hi is
      // the nearest Q4.11 word and lo the signed remainder.  Writing the raw
      // 24-bit code here biased every lane whose bit 7 was set by -256 codes
      // (2^-11), which is exactly the KDA state error measured on the revised
      // RTL (residual ~181 Q4.19 codes, per-lane -256).
      if (q > 80'sd8388607)       acc_wout[i*24 +: 24] = 24'h7FFF7F;
      else if (q < -80'sd8388608) acc_wout[i*24 +: 24] = 24'h800000;
      else                        acc_wout[i*24 +: 24] = 24'(canon24f(64'(q)));
    end
  end
endmodule

// ---------------------------------------------------------------------------
// Shared ALU: 48 lanes of 16-bit elementwise integer work.
module alu48 (
  input  logic [2:0]         op,
  input  logic [4:0]         sh,
  input  logic signed [15:0] s,
  input  logic [48*16-1:0]   a,
  input  logic [48*16-1:0]   b,
  input  logic [48*8-1:0]    a_lo,
  input  logic [48*8-1:0]    b_lo,
  input  logic               wide,
  input  logic               mul24,
  output logic [48*16-1:0]   y,
  output logic [48*8-1:0]    y_lo
);
  always_comb begin
    for (int i = 0; i < 48; i++) begin
      logic signed [15:0] ai;
      logic signed [15:0] bi;
      logic signed [47:0] p;
      logic signed [47:0] q;
      logic signed [47:0] res;
      logic signed [47:0] rem;
      logic signed [47:0] half;
      logic               up;
      logic signed [23:0] a24;
      logic signed [23:0] b24;
      logic signed [63:0] pw;
      logic signed [63:0] qw;
      logic signed [63:0] rw;
      logic signed [63:0] hw;
      logic               uw;
      logic signed [63:0] rw64;
      logic [23:0]        c24;
      ai = $signed(a[i*16 +: 16]);
      bi = $signed(b[i*16 +: 16]);
      a24 = join24(a[i*16 +: 16], a_lo[i*8 +: 8]);
      b24 = join24(b[i*16 +: 16], b_lo[i*8 +: 8]);
      p = 48'sd0;
      q = 48'sd0;
      rem = 48'sd0;
      half = 48'sd0;
      up = 1'b0;
      res = 48'sd0;
      pw = 64'sd0;
      qw = 64'sd0;
      rw = 64'sd0;
      hw = 64'sd0;
      uw = 1'b0;
      rw64 = 64'sd0;
      c24 = 24'd0;
      if (wide) begin
        case (op)
          3'd0: rw64 = $signed({{40{a24[23]}}, a24}) + $signed({{40{b24[23]}}, b24});
          3'd1: rw64 = $signed({{40{a24[23]}}, a24}) - $signed({{40{b24[23]}}, b24});
          3'd4: rw64 = (a24 > b24) ? $signed({{40{a24[23]}}, a24})
                                   : $signed({{40{b24[23]}}, b24});
          3'd5: rw64 = (a24 < b24) ? $signed({{40{a24[23]}}, a24})
                                   : $signed({{40{b24[23]}}, b24});
          3'd6: rw64 = $signed({{40{a24[23]}}, a24});
          3'd7: rw64 = -$signed({{40{a24[23]}}, a24});
          default: begin
            // op2 is a per-lane 24x16 multiply (the second operand is a Q1.14
            // gain word), op3 is a 24x16 multiply by the scalar `s`.
            if (op == 3'd2) begin
              if (mul24) pw = $signed({{40{a24[23]}}, a24}) * $signed({{40{b24[23]}}, b24});
              else     pw = $signed({{40{a24[23]}}, a24}) * $signed({{48{bi[15]}}, bi});
            end else   pw = $signed({{40{a24[23]}}, a24}) * $signed({{48{s[15]}}, s});
            if (sh == 0) qw = pw;
            else begin
              qw = pw >>> sh;
              rw = pw & ((64'sd1 << sh) - 64'sd1);
              hw = 64'sd1 << (sh - 5'd1);
              uw = (rw > hw) | ((rw == hw) & qw[0]);
              qw = qw + (uw ? 64'sd1 : 64'sd0);
            end
            rw64 = qw;
          end
        endcase
        // ops 0/1 are exact adds/subs; a non-zero sh re-expresses the result
        // (a 16-bit Q4.12 word widened to 2^-20 scale -> canonical Q4.19)
        if ((op == 3'd0 || op == 3'd1) && sh != 5'd0) begin
          qw = rw64 >>> sh;
          rw = rw64 & ((64'sd1 << sh) - 64'sd1);
          hw = 64'sd1 << (sh - 5'd1);
          uw = (rw > hw) | ((rw == hw) & qw[0]);
          rw64 = qw + (uw ? 64'sd1 : 64'sd0);
        end
        c24 = canon24f(rw64);
        y[i*16 +: 16] = c24[23:8];
        y_lo[i*8 +: 8] = c24[7:0];
      end else begin
        case (op)
          3'd0: res = $signed({ai[15], ai}) + $signed({bi[15], bi});
          3'd1: res = $signed({ai[15], ai}) - $signed({bi[15], bi});
          3'd4: res = (ai > bi) ? $signed({ai[15], ai}) : $signed({bi[15], bi});
          3'd5: res = (ai < bi) ? $signed({ai[15], ai}) : $signed({bi[15], bi});
          3'd6: res = $signed({ai[15], ai});
          3'd7: res = -$signed({ai[15], ai});
          default: begin
            if (op == 3'd2) p = $signed(ai) * $signed(bi);
            else            p = $signed(ai) * $signed(s);
            if (sh == 0) begin
              q = p;
            end else begin
              q    = p >>> sh;
              rem  = p & ((48'sd1 << sh) - 48'sd1);
              half = 48'sd1 << (sh - 5'd1);
              up   = (rem > half) | ((rem == half) & q[0]);
              q    = q + (up ? 48'sd1 : 48'sd0);
            end
            res = q;
          end
        endcase
        if (res > 48'sd32767)       y[i*16 +: 16] = 16'sh7FFF;
        else if (res < -48'sd32768) y[i*16 +: 16] = 16'sh8000;
        else                        y[i*16 +: 16] = res[15:0];
        y_lo[i*8 +: 8] = 8'd0;
      end
    end
  end
endmodule

// ---------------------------------------------------------------------------
// Nonlinear lanes: 16 lanes of exp / sigmoid / SiLU on Q4.11 inputs.
module nlin16 (
  input  logic [1:0]        op,
  input  logic [4:0]        osh,
  input  logic [16*16-1:0]  a,
  output logic [16*16-1:0]  y
);
  always_comb begin
    for (int i = 0; i < 16; i++) begin
      logic signed [15:0] x;
      logic signed [15:0] xs;
      logic signed [47:0] ev;
      logic [47:0]        d17;
      logic [31:0]        sg;
      logic signed [47:0] pv;
      logic signed [47:0] qv;
      logic signed [47:0] rem;
      logic signed [47:0] half;
      logic               upv;
      logic signed [47:0] outv;
      x = $signed(a[i*16 +: 16]);
      // op 3 is SiLU on a Q4.12 operand: the sigmoid slope still uses Q4.11
      xs = (op == 2'd3) ? (x >>> 1) : x;
      ev = 48'sd0;
      d17 = 48'sd0;
      sg = 32'd0;
      pv = 48'sd0;
      qv = 48'sd0;
      rem = 48'sd0;
      half = 48'sd0;
      upv = 1'b0;
      outv = 48'sd0;
      if (op == 2'd0) begin
        ev = exp_core(x, osh);
        outv = (ev > 48'sd32767) ? 48'sd32767 : ev;
      end else begin
        ev = exp_core((xs > 16'sd0) ? $signed(-xs) : xs, 5'd15);
        d17 = 48'd32768 + ev[16:0];
        sg = recip_f(d17, 6'd30);
        if (xs < 16'sd0) sg = (ev[16:0] * sg) >> 15;
        if (sg > 32'd32767) sg = 32'd32767;
        if (op == 2'd1) begin
          outv = $signed({16'b0, sg[15:0]});
        end else begin
          pv = $signed(x) * $signed({17'b0, sg[15:0]});
          qv = pv >>> 15;
          rem  = pv & 48'sd32767;
          half = 48'sd16384;
          upv  = (rem > half) | ((rem == half) & qv[0]);
          qv = qv + (upv ? 48'sd1 : 48'sd0);
          outv = (qv > 48'sd32767) ? 48'sd32767
               : (qv < -48'sd32768) ? -48'sd32768 : qv;
        end
      end
      y[i*16 +: 16] = outv[15:0];
    end
  end
endmodule

// ---------------------------------------------------------------------------
// Special function unit (single lane, combinational).
//   op 0 RSQRT : din = M, dout = rne(2^25 / sqrt(M))
//   op 1 RECIP : din = X, sh2 = k, dout = rne(2^k / X)
//   op 2 MULC  : dout = rne(din * din2 / 2^sh2)   (both operands are signed:
//                attention scores and context accumulators are negative for
//                roughly half of the lanes)
module sfu (
  input  logic [1:0]        op,
  input  logic [47:0]       din,
  input  logic [47:0]       din2,
  input  logic [5:0]        sh2,
  output logic [47:0]       dout
);
  logic signed [63:0] pmul;
  logic signed [63:0] bmul;
  logic signed [63:0] rmul;
  logic signed [63:0] hmul;
  logic        umul;

  always_comb begin
    dout = 48'd0;
    pmul = 64'sd0;
    bmul = 64'sd0;
    rmul = 64'sd0;
    hmul = 64'sd0;
    umul = 1'b0;
    case (op)
      2'd0: dout = rsqrt_f(din);
      2'd1: dout = {16'b0, recip_f(din, sh2)};
      default: begin
        pmul = $signed(din) * $signed(din2);
        if (sh2 == 0) begin
          bmul = pmul;
        end else begin
          bmul = pmul >>> sh2;
          // arithmetic shift keeps the remainder non-negative, so the shared
          // nearest-even rounding works for negative products as well
          rmul = pmul & ((64'sd1 << sh2) - 64'sd1);
          hmul = 64'sd1 << (sh2 - 6'd1);
          umul = (rmul > hmul) | ((rmul == hmul) & bmul[0]);
          bmul = bmul + (umul ? 64'sd1 : 64'sd0);
        end
        dout = bmul[47:0];
      end
    endcase
  end
endmodule

// ---------------------------------------------------------------------------
// Reduction over one 64-lane vector: 0 sum, 1 sum of squares, 2 max, 3 min
module red64 (
  input  logic [1:0]         op,
  input  logic [1023:0]      a,
  input  logic [511:0]       a_lo,
  output logic signed [47:0] r,
  output logic signed [55:0] sq_wide
);
  always_comb begin
    logic signed [47:0] acc_s;
    logic signed [47:0] mx;
    logic signed [47:0] mn;
    logic signed [47:0] sq;
    logic signed [55:0] sqw;
    acc_s = 48'sd0;
    mx = -48'sh7FFFFFFFFFFF;
    mn = 48'sh7FFFFFFFFFFF;
    sq = 48'sd0;
    sqw = 56'sd0;
    for (int i = 0; i < 64; i++) begin
      logic signed [15:0] v;
      logic signed [23:0] v24;
      v = $signed(a[i*16 +: 16]);
      v24 = join24(a[i*16 +: 16], a_lo[i*8 +: 8]);
      acc_s = acc_s + v;
      sq = sq + (v * v);
      sqw = sqw + ($signed({{40{v24[23]}}, v24}) * $signed({{40{v24[23]}}, v24}));
      if ($signed(v) > mx) mx = $signed(v);
      if ($signed(v) < mn) mn = $signed(v);
    end
    case (op)
      2'd0: r = acc_s;
      2'd1: r = sq;
      2'd2: r = mx;
      default: r = mn;
    endcase
    sq_wide = sqw;
  end
endmodule
