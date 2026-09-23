// KDA recurrence after the six q4 projections have been materialized.
// It owns three pre-conv histories, L2 q/k normalization, b/f/A/dtb decay,
// state update/read, onorm and the g1/g2 sigmoid gate. The caller applies the
// final q4 `L*.kda.o` projection to `core_bus`.
module kda_recurrence_contract #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic clk, input logic rst_n, input logic step_valid,
  input logic signed [64*DATA_W-1:0] q_pre_bus, k_pre_bus, v_pre_bus,
  input logic signed [64*4*16-1:0] qconv_bus, kconv_bus, vconv_bus,
  input logic signed [2*DATA_W-1:0] b_bus,
  input logic signed [64*DATA_W-1:0] f_bus, z_bus,
  input logic [2*16-1:0] A_bus,                    // Q8.8 unsigned
  input logic signed [64*16-1:0] dtb_bus,          // Q4.11
  input logic signed [32*16-1:0] onorm_offset_bus, // Q2.13, shared per head
  output logic signed [64*DATA_W-1:0] core_bus
);
  localparam int DH = 32;
  logic signed [64*DATA_W-1:0] q_conv, k_conv, v_conv, q_act, k_act, v_act;
  logic signed [64*DATA_W-1:0] q_norm, k_norm, alpha_bus, head_raw, head_norm;
  logic [64*(FRAC_W+1)-1:0] z_gate;
  logic [2*(FRAC_W+1)-1:0] beta_gate;
  logic signed [2*DATA_W-1:0] beta_bus;
  integer i, h;

  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) qconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(q_pre_bus), .taps_bus(qconv_bus), .pre_silu_bus(q_conv));
  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) kconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(k_pre_bus), .taps_bus(kconv_bus), .pre_silu_bus(k_conv));
  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) vconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(v_pre_bus), .taps_bus(vconv_bus), .pre_silu_bus(v_conv));
  generate
    genvar lane;
    for (lane = 0; lane < 64; lane = lane + 1) begin : nonlinear
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sq (.x_in(q_conv[lane*DATA_W +: DATA_W]), .y_out(q_act[lane*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sk (.x_in(k_conv[lane*DATA_W +: DATA_W]), .y_out(k_act[lane*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sv (.x_in(v_conv[lane*DATA_W +: DATA_W]), .y_out(v_act[lane*DATA_W +: DATA_W]));
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sz (.x_in(z_bus[lane*DATA_W +: DATA_W]), .y_out(z_gate[lane*(FRAC_W+1) +: FRAC_W+1]));
    end
    for (lane = 0; lane < 2; lane = lane + 1) begin : beta_nonlinear
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sb (.x_in(b_bus[lane*DATA_W +: DATA_W]), .y_out(beta_gate[lane*(FRAC_W+1) +: FRAC_W+1]));
      l2norm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) nq
        (.x_bus(q_act[lane*DH*DATA_W +: DH*DATA_W]), .y_bus(q_norm[lane*DH*DATA_W +: DH*DATA_W]));
      l2norm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) nk
        (.x_bus(k_act[lane*DH*DATA_W +: DH*DATA_W]), .y_bus(k_norm[lane*DH*DATA_W +: DH*DATA_W]));
      kda_delta_head #(.DH(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) delta_state
        (.clk, .rst_n, .step_valid, .q_in(q_norm[lane*DH*DATA_W +: DH*DATA_W]),
         .k_in(k_norm[lane*DH*DATA_W +: DH*DATA_W]), .v_in(v_act[lane*DH*DATA_W +: DH*DATA_W]),
         .alpha_in(alpha_bus[lane*DH*DATA_W +: DH*DATA_W]), .beta_in(beta_bus[lane*DATA_W +: DATA_W]),
         .out_bus(head_raw[lane*DH*DATA_W +: DH*DATA_W]));
      rmsnorm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W + 4), .EPS_Q2FRAC(64'd687195)) no
        (.x_bus(head_raw[lane*DH*DATA_W +: DH*DATA_W]), .norm_offset_bus(onorm_offset_bus),
         .y_bus(head_norm[lane*DH*DATA_W +: DH*DATA_W]));
    end
  endgenerate

  // alpha path per lane:  alpha_product = A * (f + (dtb <<< 3));
  // gate_argument = alpha_product >>> 8; gate_sigmoid = sigmoid_from_q14;
  // alpha_bus = decay_from_sigmoid.  The two PWL fits are parameterized
  // modules instantiated once per lane (2 heads x 32 lanes) below, so each
  // lerp forest is synthesized as a single module body shared by the 64
  // instances instead of being function-inlined 64 times into this module
  // (which previously exploded the techmap cell count and stalled).

  // Natural product width: 24-bit head_norm x 16-bit z_gate => 40-bit signed
  // product, exact.  Was [63:0]: 64-bit LHS widened the multiply the same way.
  logic signed [DATA_W+15:0] output_product;
  generate
    genvar hh, ii;
    for (hh = 0; hh < 2; hh = hh + 1) begin : alpha_head
      for (ii = 0; ii < DH; ii = ii + 1) begin : alpha_lane
        logic signed [DATA_W-1:0] gate_argument;
        logic [FRAC_W:0] gate_sigmoid;
        // Natural product width: 17-bit A (unsigned zero-ext to signed) x
        // 24-bit (f + dtb<<<3) => 41-bit signed product, exact, no overflow.
        // Was [63:0]: the 64-bit LHS forced Yosys/ABC to build 64x64 signed
        // multipliers (zero-extended A, 64-bit add), exploding techmap.
        logic signed [DATA_W+16:0] alpha_product;
        always_comb begin
          alpha_product = $signed({1'b0, A_bus[hh*16 +: 16]}) *
                    ($signed(f_bus[(hh*DH+ii)*DATA_W +: DATA_W]) + ($signed(dtb_bus[(hh*DH+ii)*16 +: 16]) <<< 3));
          gate_argument = alpha_product >>> 8;
        end
        sigmoid_from_q14_mod #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sigmoid_inst (
          .x_in(gate_argument),
          .y_out(gate_sigmoid)
        );
        decay_from_sigmoid_mod #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) decay_inst (
          .s_in(gate_sigmoid),
          .y_out(alpha_bus[(hh*DH+ii)*DATA_W +: DATA_W])
        );
      end
    end
  endgenerate
  always_comb begin
    beta_bus = '0;
    for (h = 0; h < 2; h = h + 1)
      beta_bus[h*DATA_W +: DATA_W] = $signed({1'b0, beta_gate[h*(FRAC_W+1) +: FRAC_W+1]});
  end
  always_comb begin
    core_bus = '0;
    for (h = 0; h < 2; h = h + 1) begin
      for (i = 0; i < DH; i = i + 1) begin
        // head_norm is Q*.18 (from the wider onorm); z_gate is Q1.14.  One
        // 18-bit rescale returns core to the external Q*.14 convention.
        output_product = $signed(head_norm[(h*DH+i)*DATA_W +: DATA_W]) *
                  $signed({1'b0, z_gate[(h*DH+i)*(FRAC_W+1) +: FRAC_W+1]});
        core_bus[(h*DH+i)*DATA_W +: DATA_W] = output_product >>> (FRAC_W + 4);
      end
    end
  end
endmodule

// Equivalent parameterized-module form of the former automatic function
// sigmoid_from_q14 in kda_recurrence_contract.  Dense sigmoid PWL fit,
// input signed Q*.14, output unsigned Q1.14.  Bit-exact to the inlined
// function: x_in (signed DATA_W) sign-extends into the 64-bit working x,
// and the PWL breakpoints/lerp are unchanged.
module sigmoid_from_q14_mod #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic        [FRAC_W:0]   y_out
);
  logic signed [63:0] x;
  logic signed [63:0] y;
  logic signed [63:0] seg_x0, seg_x1, seg_y0, seg_y1;
  logic signed [63:0] seg_idx;
  logic lerp_sel;

  // Division-free lerp: every segment width is a power of two (2048/4096/
  // 32768), so the runtime divide is replaced by an arithmetic shift by
  // k=log2(x1-x0).  With dx in [0,32768] and |dy|<=511 the true product is
  // well below 2^31, and the rounding-bias shift reproduces
  // truncation-toward-zero signed division by a power of two bit-exactly.
  function automatic logic signed [63:0] lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    logic signed [16:0] dx;
    logic signed [9:0]  dy;
    logic signed [31:0] p, w, bias, q;
    logic [4:0] k;
    w    = x1 - x0;                    // 2^k, k in {11,12,15}
    k    = (w == 2048) ? 5'd11 : (w == 4096) ? 5'd12 : 5'd15;
    dx   = (x_value - x0);             // [0,32768] when a segment is selected
    dy   = (y1 - y0);                  // |dy| <= 511
    p    = dx * dy;                    // |p| < 2^31, no overflow
    bias = p[31] ? (w - 32'sd1) : 32'sd0;
    q    = (p + bias) >>> k;
    lerp = y0 + q;
  endfunction

  // Endpoint lookup for the sigmoid fit, 64-bit working width.  The fit uses
  // three segment spacings: 2048-wide in the central [-65536,65536] band,
  // 4096-wide in the [-98304,-65536] and [65536,98304] wings, and a single
  // 32768-wide bottom [-131072,-98304] step kept as an explicit segment
  // below.  The table below stores the fit value at each breakpoint,
  // transcribed verbatim from the previous if-else chain's segment y0/y1
  // constants; adjacent segments share their boundary endpoint, so a
  // breakpoint selects the same lerp() result from either side.
  // A single case-based endpoint table keeps this leaf structurally
  // isomorphic to the proven-working silu_pwl lookup (one y_tab function +
  // one-index mux), while collapsing the 80-way priority chain.  The
  // breakpoints are a monotone sigmoid sample set: the left wing shares its
  // -65536 end (295) with the central band's first sample, and the central
  // band shares its +65536 end (16089) with the right wing's first sample,
  // so all three regions index into one flat table.  Values are transcribed
  // verbatim from the previous three-table version; adjacent segments share
  // their boundary endpoint, so a breakpoint selects the same lerp() result
  // from either side.
  function automatic logic signed [63:0] y_tab(input integer i);
    case (i)
      0: y_tab = 41;   1: y_tab = 52;   2: y_tab = 67;   3: y_tab = 86;
      4: y_tab = 110;  5: y_tab = 141;  6: y_tab = 180;  7: y_tab = 230;
      8: y_tab = 295;  9: y_tab = 333;  10: y_tab = 376; 11: y_tab = 425;
      12: y_tab = 480; 13: y_tab = 542; 14: y_tab = 612; 15: y_tab = 690;
      16: y_tab = 777; 17: y_tab = 875; 18: y_tab = 984; 19: y_tab = 1107;
      20: y_tab = 1243; 21: y_tab = 1394; 22: y_tab = 1562; 23: y_tab = 1748;
      24: y_tab = 1953; 25: y_tab = 2178; 26: y_tab = 2426; 27: y_tab = 2695;
      28: y_tab = 2989; 29: y_tab = 3307; 30: y_tab = 3649; 31: y_tab = 4015;
      32: y_tab = 4406; 33: y_tab = 4820; 34: y_tab = 5256; 35: y_tab = 5712;
      36: y_tab = 6186; 37: y_tab = 6674; 38: y_tab = 7173; 39: y_tab = 7681;
      40: y_tab = 8192; 41: y_tab = 8703; 42: y_tab = 9211; 43: y_tab = 9710;
      44: y_tab = 10198; 45: y_tab = 10672; 46: y_tab = 11128; 47: y_tab = 11564;
      48: y_tab = 11978; 49: y_tab = 12369; 50: y_tab = 12735; 51: y_tab = 13077;
      52: y_tab = 13395; 53: y_tab = 13689; 54: y_tab = 13958; 55: y_tab = 14206;
      56: y_tab = 14431; 57: y_tab = 14636; 58: y_tab = 14822; 59: y_tab = 14990;
      60: y_tab = 15141; 61: y_tab = 15277; 62: y_tab = 15400; 63: y_tab = 15509;
      64: y_tab = 15607; 65: y_tab = 15694; 66: y_tab = 15772; 67: y_tab = 15842;
      68: y_tab = 15904; 69: y_tab = 15959; 70: y_tab = 16008; 71: y_tab = 16051;
      72: y_tab = 16089; 73: y_tab = 16154; 74: y_tab = 16204; 75: y_tab = 16243;
      76: y_tab = 16274; 77: y_tab = 16298; 78: y_tab = 16317; 79: y_tab = 16332;
      80: y_tab = 16343;
      default: y_tab = 0;
    endcase
  endfunction

  always_comb begin
    x = x_in;
    y = '0;
    seg_x0 = '0; seg_x1 = '0; seg_y0 = '0; seg_y1 = '0;
    seg_idx = '0;
    lerp_sel = 1'b0;
    if (x <= -131072) y = 5;
    else if (x <= -98304) begin seg_x0 = -131072; seg_x1 = -98304; seg_y0 = 5; seg_y1 = 41; lerp_sel = 1'b1; end
    else if (x <= -65536) begin
      // Left wing (-98304, -65536]: 8 x 4096-wide segments; global table
      // index equals the local segment index (breakpoints -98304 + i*4096).
      seg_idx = (x + 64'sd98304) >>> 12;
      seg_x0  = -64'sd98304 + (seg_idx <<< 12);
      seg_x1  = seg_x0 + 64'sd4096;
      seg_y0  = y_tab(seg_idx);
      seg_y1  = y_tab(seg_idx + 64'sd1);
      lerp_sel = 1'b1;
    end
    else if (x <= 65536) begin
      // Central fine band (-65536, 65536]: 64 x 2048-wide segments.  The
      // band's first breakpoint (-65536 -> 295) is table index 8 (shared
      // with the left wing), so local segment i maps to table indices 8+i/9+i.
      seg_idx = (x + 64'sd65535) >>> 11;
      seg_x0  = -64'sd65536 + (seg_idx <<< 11);
      seg_x1  = seg_x0 + 64'sd2048;
      seg_y0  = y_tab(8 + seg_idx);
      seg_y1  = y_tab(9 + seg_idx);
      lerp_sel = 1'b1;
    end
    else if (x <= 98304) begin
      // Right wing (65536, 98304]: 8 x 4096-wide segments.  Its first
      // breakpoint (+65536 -> 16089) is table index 72 (shared with the
      // central band), so local segment k maps to table indices 72+k/73+k.
      seg_idx = (x - 64'sd65536) >>> 12;
      seg_x0  = 64'sd65536 + (seg_idx <<< 12);
      seg_x1  = seg_x0 + 64'sd4096;
      seg_y0  = y_tab(72 + seg_idx);
      seg_y1  = y_tab(73 + seg_idx);
      lerp_sel = 1'b1;
    end
    else y = 16384;
    if (lerp_sel) y = lerp(x, seg_x0, seg_x1, seg_y0, seg_y1);
    y_out = y[FRAC_W:0];
  end
endmodule

// Equivalent parameterized-module form of the former automatic function
// decay_from_sigmoid in kda_recurrence_contract.  PWL for exp(-5*s), s in
// unsigned Q1.14, output signed DATA_W.  Bit-exact to the inlined function:
// s_in zero-extends through $signed({1'b0, s_in}), the clamp y>=110 and the
// lerp breakpoints are unchanged.
//
// Round-11 synthesis-usability rewrite (same PWL, same 50 segment
// breakpoints, same truncation-toward-zero lerp): the old 64-bit working
// width + 50-way 64-bit priority chain + 64x64 multiply / 64-bit runtime
// divide expanded to 501,651 gate cells and stalled ABC at "extracting gate
// netlist" inside the 180 s window (round-10 log).  This mirrors the
// proven-working sigmoid_from_q14_mod / silu_pwl structure: 32-bit signed
// working width, the priority chain collapsed to a 6-level binary search over
// a 50-entry breakpoint table plus a shared endpoint table.  |s-x0| <= 16711
// and |y1-y0| <= 1559 keep the lerp product < 2^31, so every 32-bit
// intermediate reproduces the old 64-bit arithmetic bit-exactly (same
// numerator and same divisor => same truncating quotient).
//
// Round-32 synthesis-usability fix: the 32-bit runtime signed `p / w` (w is
// the 327/328 segment width) was the only `$div` in this PWL and the sole
// common failure point of the decay / kda_recurrence_contract partitions
// (both died at ABC techmap, round-28 attempt6 log).  It is replaced by a
// division-free, bit-exact sign-magnitude restoring long division: 32
// shift-subtract steps over |p| yield |p|/w as an unsigned truncating
// quotient, then the sign is restored.  This reproduces trunc(p/w) for every
// p in [-2^31, 2^31) and w in {327,328} without emitting a `$div` cell.
module decay_from_sigmoid_mod #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic        [FRAC_W:0]   s_in,
  output logic signed [DATA_W-1:0] y_out
);
  logic signed [31:0] s;
  logic signed [31:0] x0, x1, y0, y1, w, p, q, y;
  logic signed [16:0] dx, dy;
  logic [6:0] lo, hi, mid, seg;
  integer di;
  logic p_neg;
  logic [31:0] p_mag, rem, q_mag, w_u;

  // Breakpoints b(i): x1 of segment i-1 (== x0 of segment i); b(0)=0 and
  // b(49)=16056.  Transcribed verbatim from the old chain's seg_x1 constants.
  // Segment i (0..48) covers (b(i), b(i+1)]; s > b(49) uses the extrapolated
  // tail [16056,16384] exactly as the old final `else` did.
  function automatic logic signed [31:0] b_tab(input integer i);
    case (i)
      0: b_tab = 0;      1: b_tab = 328;   2: b_tab = 655;   3: b_tab = 983;
      4: b_tab = 1311;   5: b_tab = 1638;  6: b_tab = 1966;  7: b_tab = 2294;
      8: b_tab = 2621;   9: b_tab = 2949;  10: b_tab = 3277; 11: b_tab = 3604;
      12: b_tab = 3932;  13: b_tab = 4260; 14: b_tab = 4588; 15: b_tab = 4915;
      16: b_tab = 5243;  17: b_tab = 5571; 18: b_tab = 5898; 19: b_tab = 6226;
      20: b_tab = 6554;  21: b_tab = 6881; 22: b_tab = 7209; 23: b_tab = 7537;
      24: b_tab = 7864;  25: b_tab = 8192; 26: b_tab = 8520; 27: b_tab = 8847;
      28: b_tab = 9175;  29: b_tab = 9503; 30: b_tab = 9830; 31: b_tab = 10158;
      32: b_tab = 10486; 33: b_tab = 10813; 34: b_tab = 11141; 35: b_tab = 11469;
      36: b_tab = 11796; 37: b_tab = 12124; 38: b_tab = 12452; 39: b_tab = 12780;
      40: b_tab = 13107; 41: b_tab = 13435; 42: b_tab = 13763; 43: b_tab = 14090;
      44: b_tab = 14418; 45: b_tab = 14746; 46: b_tab = 15073; 47: b_tab = 15401;
      48: b_tab = 15729; 49: b_tab = 16056;
      default: b_tab = 0;
    endcase
  endfunction

  // Fit value at each breakpoint, y(b(i)).  Transcribed verbatim from the old
  // chain's seg_y0 constants; the fit is continuous, so seg_y1(i) ==
  // y_tab(i+1).  The tail's y1 = 110 is a literal below.
  function automatic logic signed [31:0] y_tab(input integer i);
    case (i)
      0: y_tab = 16384; 1: y_tab = 14825; 2: y_tab = 13414; 3: y_tab = 12138;
      4: y_tab = 10983; 5: y_tab = 9937;  6: y_tab = 8992;  7: y_tab = 8136;
      8: y_tab = 7362;  9: y_tab = 6661;  10: y_tab = 6027; 11: y_tab = 5454;
      12: y_tab = 4935; 13: y_tab = 4465; 14: y_tab = 4040; 15: y_tab = 3656;
      16: y_tab = 3308; 17: y_tab = 2993; 18: y_tab = 2708; 19: y_tab = 2451;
      20: y_tab = 2217; 21: y_tab = 2006; 22: y_tab = 1815; 23: y_tab = 1643;
      24: y_tab = 1486; 25: y_tab = 1345; 26: y_tab = 1217; 27: y_tab = 1101;
      28: y_tab = 996;  29: y_tab = 902;  30: y_tab = 816;  31: y_tab = 738;
      32: y_tab = 668;  33: y_tab = 604;  34: y_tab = 547;  35: y_tab = 495;
      36: y_tab = 448;  37: y_tab = 405;  38: y_tab = 367;  39: y_tab = 332;
      40: y_tab = 300;  41: y_tab = 272;  42: y_tab = 246;  43: y_tab = 222;
      44: y_tab = 201;  45: y_tab = 182;  46: y_tab = 165;  47: y_tab = 149;
      48: y_tab = 135;  49: y_tab = 122;
      default: y_tab = 0;
    endcase
  endfunction

  always_comb begin
    s = $signed({1'b0, s_in});
    // Segment select: smallest i in 0..48 with s <= b(i+1); s > b(49) -> 49.
    // Six unrolled bisection steps close the 50-entry range (2^6 >= 50), so
    // only 6 comparison levels feed the endpoint tables instead of a 50-deep
    // priority chain of endpoint muxes.
    lo = 7'd0; hi = 7'd49; mid = 7'd0;
    if (s > b_tab(49)) seg = 7'd49;
    else begin
      lo = 7'd0; hi = 7'd49;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      mid = (lo + hi) >> 1; if (s <= b_tab(mid + 1)) hi = mid; else lo = mid + 1;
      seg = lo;
    end
    x0 = b_tab(seg);
    x1 = (seg == 7'd49) ? 32'sd16384 : b_tab(seg + 1);
    y0 = y_tab(seg);
    y1 = (seg == 7'd49) ? 32'sd110 : y_tab(seg + 1);
    // Narrow lerp: dx in [0,16711], |dy| <= 1559, so |p| < 2^31 and the 32-bit
    // multiply plus a truncating divide by the same 327/328 width w reproduce
    // the old 64-bit lerp bit-exactly.  The divide is emitted as a restoring
    // long division (no `$div` cell): |p|/w by 32 shift-subtract steps with
    // the sign restored on the quotient, == trunc(p/w) bit-for-bit.
    dx = s - x0;
    dy = y1 - y0;
    p  = dx * dy;
    w  = x1 - x0;
    p_neg = p[31];
    p_mag = p_neg ? (~p + 32'd1) : p;
    w_u   = w;
    rem   = 32'd0;
    q_mag = 32'd0;
    for (di = 31; di >= 0; di = di - 1) begin
      rem = {rem[30:0], p_mag[di]};
      if (rem >= w_u) begin
        rem       = rem - w_u;
        q_mag[di] = 1'b1;
      end
    end
    q = $signed(p_neg ? (~q_mag + 32'd1) : q_mag);
    y  = y0 + q;
    if (y < 32'sd110) y = 32'sd110;
    y_out = y;
  end
endmodule
