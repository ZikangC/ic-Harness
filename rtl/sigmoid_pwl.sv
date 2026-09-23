// Shared sigmoid approximation. Input is signed Q*.14; output is unsigned
// Q1.14. Breakpoints are symmetric, monotonic PWL samples of sigmoid(x).
//
// The previous dense fit still left ~0.007 error at |x|~2.4 (0.5/1.0 spacing
// between -2 and -1.5, and between 2 and 3).  That error is amplified by the
// KDA alpha path (alpha = exp(-5*sigmoid(...)), d alpha/d sigmoid = -5*alpha)
// and directly by the KDA z gate (core = head_norm * sigmoid(z)).  This fit
// samples sigmoid every 0.125 over the critical [-4, 4] span and every 0.25
// in the [-6,-4]/[4,6] tails, removing that error source while keeping the
// same Q1.14 output contract for the residual-mix weights, KDA z/beta gates
// and the legacy smoke adapter.
module sigmoid_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic        [FRAC_W:0]   y_out
);
  logic signed [31:0] x;
  logic signed [31:0] y;
  logic signed [31:0] seg_x0, seg_x1, seg_y0, seg_y1;
  logic signed [31:0] seg_idx;
  logic lerp_sel;
  // Narrowed to 32-bit signed (was 64-bit).  Only the selected segment's lerp
  // is computed: the if-else chain below just muxes the segment endpoints and
  // the single trailing lerp() call does one multiply and one power-of-two
  // arithmetic shift (no runtime divider).  |x_value-x0| <= 32768 and max
  // |y1-y0| = 511 here: the product is well below 2^31 and the quotient
  // bit-identical to the 32-bit signed divide.
  localparam logic signed [31:0] ONE = 32'sd1 <<< FRAC_W;

  // Division-free lerp: every segment width is a power of two (2048/4096/
  // 32768), so the runtime divide is replaced by an arithmetic shift by
  // k=log2(x1-x0).  With dx in [0,32768] and |dy|<=511 the true product is
  // well below 2^31, and the rounding-bias shift reproduces
  // truncation-toward-zero signed division by a power of two bit-exactly.
  function automatic logic signed [31:0] lerp(
      input logic signed [31:0] x_value,
      input logic signed [31:0] x0,
      input logic signed [31:0] x1,
      input logic signed [31:0] y0,
      input logic signed [31:0] y1
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

  // Endpoint lookup for the sigmoid fit.  The fit uses three segment
  // spacings: 2048-wide in the central [-65536,65536] band, 4096-wide in the
  // [-98304,-65536] and [65536,98304] wings, and a single 32768-wide bottom
  // [-131072,-98304] step kept as an explicit segment below.  The table
  // below stores the fit value at each breakpoint, transcribed verbatim from
  // the previous if-else chain's segment y0/y1 constants; adjacent segments
  // share their boundary endpoint, so a breakpoint selects the same lerp()
  // result from either side.
  // A single case-based endpoint table keeps this module structurally
  // isomorphic to the proven-working silu_pwl lookup (one y_tab function +
  // one-index mux), while collapsing the 80-way priority chain.  The breakpoints
  // are a monotone sigmoid sample set: the left wing shares its -65536 end
  // (295) with the central band's first sample, and the central band shares
  // its +65536 end (16089) with the right wing's first sample, so all three
  // regions index into one flat table.  Values are transcribed verbatim from
  // the previous three-table version; adjacent segments share their boundary
  // endpoint, so a breakpoint selects the same lerp() result from either side.
  function automatic logic signed [31:0] y_tab(input integer i);
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
      seg_idx = (x + 32'sd98304) >>> 12;
      seg_x0  = -32'sd98304 + (seg_idx <<< 12);
      seg_x1  = seg_x0 + 32'sd4096;
      seg_y0  = y_tab(seg_idx);
      seg_y1  = y_tab(seg_idx + 32'sd1);
      lerp_sel = 1'b1;
    end
    else if (x <= 65536) begin
      // Central fine band (-65536, 65536]: 64 x 2048-wide segments.  The
      // band's first breakpoint (-65536 -> 295) is table index 8 (shared
      // with the left wing), so local segment i maps to table indices 8+i/9+i.
      seg_idx = (x + 32'sd65535) >>> 11;
      seg_x0  = -32'sd65536 + (seg_idx <<< 11);
      seg_x1  = seg_x0 + 32'sd2048;
      seg_y0  = y_tab(8 + seg_idx);
      seg_y1  = y_tab(9 + seg_idx);
      lerp_sel = 1'b1;
    end
    else if (x <= 98304) begin
      // Right wing (65536, 98304]: 8 x 4096-wide segments.  Its first
      // breakpoint (+65536 -> 16089) is table index 72 (shared with the
      // central band), so local segment k maps to table indices 72+k/73+k.
      seg_idx = (x - 32'sd65536) >>> 12;
      seg_x0  = 32'sd65536 + (seg_idx <<< 12);
      seg_x1  = seg_x0 + 32'sd4096;
      seg_y0  = y_tab(72 + seg_idx);
      seg_y1  = y_tab(73 + seg_idx);
      lerp_sel = 1'b1;
    end
    else y = 16384;
    if (lerp_sel) y = lerp(x, seg_x0, seg_x1, seg_y0, seg_y1);
    y_out = y[FRAC_W:0];
  end
endmodule
