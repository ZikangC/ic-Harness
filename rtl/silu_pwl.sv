// SiLU(x) = x * sigmoid(x) approximated by a direct piecewise-linear fit.
//
// The previous direct fit (0.25/0.5 spacing) still left up to ~0.007 error at
// |x|~1.2, and the 0.125-spacing fit still left ~0.001 (16 LSB) error near
// |x|~0.19.  That residual error dominates the KDA q/k/v activation error and,
// after the L2 norms and the low-magnitude onorm-amplified readout, is the
// largest single contributor to the KDA core error (and hence to the L0/L1
// routes and the final logits).  This fit samples silu every 0.0625 over the
// critical [-4, 4] span (residual error <= 4 LSB) and keeps the coarse tails
// outside it; the fit is exact at each sample and monotonic.  Breakpoints are
// in Q*.14 units of ONE.
module silu_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic signed [DATA_W-1:0] y_out
);
  // Narrowed to 32-bit signed (was 64-bit).  Only the selected segment's lerp
  // is computed: the if-else chain below just muxes the segment endpoints and
  // the single trailing lerp() call does one multiply and one power-of-two
  // arithmetic shift (no runtime divider).  |x_value-x0| <= 32768 and max
  // |y1-y0| = 33704 here: the product is < 2^31 and the quotient bit-identical
  // to the 32-bit signed divide.
  localparam logic signed [31:0] ONE = 32'sd1 <<< FRAC_W;

  // Division-free lerp: every segment width is a power of two (1024 or 32768),
  // so the runtime divide is replaced by an arithmetic shift by k=log2(x1-x0).
  // With dx in [0,32768] and |dy|<=33704 the true product is < 2^31, and the
  // rounding-bias shift reproduces truncation-toward-zero signed division by a
  // power of two bit-exactly.
  function automatic logic signed [31:0] lerp(
      input logic signed [31:0] x_value,
      input logic signed [31:0] x0,
      input logic signed [31:0] x1,
      input logic signed [31:0] y0,
      input logic signed [31:0] y1
  );
    logic signed [16:0] dx, dy;
    logic signed [31:0] p, w, bias, q;
    logic [4:0] k;
    w    = x1 - x0;                    // 2^k, k in {10,15}
    k    = (w == 1024) ? 5'd10 : 5'd15;
    dx   = (x_value - x0);             // [0,32768] when a segment is selected
    dy   = (y1 - y0);                  // |dy| <= 33704
    p    = dx * dy;                    // |p| < 2^31, no overflow
    bias = p[31] ? (w - 32'sd1) : 32'sd0;
    q    = (p + bias) >>> k;
    lerp = y0 + q;
  endfunction

  logic signed [31:0] x;
  logic signed [31:0] y;
  logic signed [31:0] seg_x0, seg_x1, seg_y0, seg_y1;
  logic lerp_sel;
  logic signed [31:0] seg_idx;

  // Endpoint lookup for the 128 uniform 1024-wide fine segments covering
  // (-65536, 65536]: y_tab(i) is the fit value at breakpoint x = -65536 + i*1024
  // for i in 0..128.  Values are transcribed verbatim from the previous if-else
  // chain's segment y0/y1 constants; adjacent segments share their boundary
  // endpoint, so a breakpoint selects the same lerp() result from either side.
  // A case-based function (rather than a packed/array localparam) keeps the
  // design parseable by the Yosys 0.64 SV frontend while still collapsing the
  // lookup into a single-index mux instead of a 128-way priority chain.
  function automatic logic signed [31:0] y_tab(input integer i);
    case (i)
      0: y_tab = -1179; 1: y_tab = -1234; 2: y_tab = -1291; 3: y_tab = -1350;
      4: y_tab = -1412; 5: y_tab = -1476; 6: y_tab = -1542; 7: y_tab = -1610;
      8: y_tab = -1681; 9: y_tab = -1754; 10: y_tab = -1830; 11: y_tab = -1907;
      12: y_tab = -1988; 13: y_tab = -2070; 14: y_tab = -2155; 15: y_tab = -2242;
      16: y_tab = -2331; 17: y_tab = -2422; 18: y_tab = -2516; 19: y_tab = -2611;
      20: y_tab = -2707; 21: y_tab = -2805; 22: y_tab = -2905; 23: y_tab = -3006;
      24: y_tab = -3107; 25: y_tab = -3209; 26: y_tab = -3311; 27: y_tab = -3413;
      28: y_tab = -3515; 29: y_tab = -3615; 30: y_tab = -3715; 31: y_tab = -3812;
      32: y_tab = -3906; 33: y_tab = -3997; 34: y_tab = -4085; 35: y_tab = -4167;
      36: y_tab = -4245; 37: y_tab = -4316; 38: y_tab = -4380; 39: y_tab = -4436;
      40: y_tab = -4483; 41: y_tab = -4520; 42: y_tab = -4546; 43: y_tab = -4560;
      44: y_tab = -4561; 45: y_tab = -4547; 46: y_tab = -4517; 47: y_tab = -4471;
      48: y_tab = -4406; 49: y_tab = -4322; 50: y_tab = -4218; 51: y_tab = -4092;
      52: y_tab = -3942; 53: y_tab = -3769; 54: y_tab = -3570; 55: y_tab = -3345;
      56: y_tab = -3093; 57: y_tab = -2812; 58: y_tab = -2503; 59: y_tab = -2163;
      60: y_tab = -1793; 61: y_tab = -1392; 62: y_tab = -960;  63: y_tab = -496;
      64: y_tab = 0;     65: y_tab = 528;   66: y_tab = 1088;  67: y_tab = 1680;
      68: y_tab = 2303;  69: y_tab = 2957;  70: y_tab = 3641;  71: y_tab = 4356;
      72: y_tab = 5099;  73: y_tab = 5871;  74: y_tab = 6670;  75: y_tab = 7495;
      76: y_tab = 8346;  77: y_tab = 9220;  78: y_tab = 10118; 79: y_tab = 11038;
      80: y_tab = 11978; 81: y_tab = 12937; 82: y_tab = 13915; 83: y_tab = 14909;
      84: y_tab = 15919; 85: y_tab = 16944; 86: y_tab = 17982; 87: y_tab = 19032;
      88: y_tab = 20093; 89: y_tab = 21164; 90: y_tab = 22244; 91: y_tab = 23332;
      92: y_tab = 24427; 93: y_tab = 25529; 94: y_tab = 26635; 95: y_tab = 27747;
      96: y_tab = 28862; 97: y_tab = 29980; 98: y_tab = 31101; 99: y_tab = 32225;
      100: y_tab = 33349; 101: y_tab = 34475; 102: y_tab = 35601; 103: y_tab = 36727;
      104: y_tab = 37853; 105: y_tab = 38978; 106: y_tab = 40103; 107: y_tab = 41226;
      108: y_tab = 42349; 109: y_tab = 43469; 110: y_tab = 44588; 111: y_tab = 45706;
      112: y_tab = 46821; 113: y_tab = 47934; 114: y_tab = 49045; 115: y_tab = 50154;
      116: y_tab = 51260; 117: y_tab = 52365; 118: y_tab = 53466; 119: y_tab = 54566;
      120: y_tab = 55663; 121: y_tab = 56758; 122: y_tab = 57850; 123: y_tab = 58940;
      124: y_tab = 60028; 125: y_tab = 61114; 126: y_tab = 62197; 127: y_tab = 63278;
      128: y_tab = 64357;
      default: y_tab = 0;
    endcase
  endfunction

  always_comb begin
    x = x_in;
    y = '0;
    seg_x0 = '0; seg_x1 = '0; seg_y0 = '0; seg_y1 = '0;
    seg_idx = '0;
    lerp_sel = 1'b0;
    // Beyond +8 silu(x) ~= x to within exp(-8); below -8 it is ~= -x*e^x ~ 0.
    if (x > 8*ONE)         y = x;
    else if (x <= -8*ONE)  y = -44;
      else if (x <= -98304) begin seg_x0 = -131072; seg_x1 = -98304; seg_y0 = -44; seg_y1 = -243; lerp_sel = 1'b1; end
      else if (x <= -65536) begin seg_x0 = -98304; seg_x1 = -65536; seg_y0 = -243; seg_y1 = -1179; lerp_sel = 1'b1; end
      else if (x <= 65536) begin
        // Fine region (-65536, 65536]: 128 uniform 1024-wide segments folded
        // from the previous 128-way priority chain into one arithmetic index
        // plus a flat endpoint table.  seg_idx = (x+65535)>>10 selects the
        // segment whose breakpoints are x = -65536 + seg_idx*1024; interior
        // breakpoints resolve to the upper segment, which shares its left
        // endpoint with the lower segment, so lerp() is bit-identical either way.
        seg_idx = (x + 32'sd65535) >>> 10;
        seg_x0  = -32'sd65536 + (seg_idx <<< 10);
        seg_x1  = seg_x0 + 32'sd1024;
        seg_y0  = y_tab(seg_idx);
        seg_y1  = y_tab(seg_idx + 32'sd1);
        lerp_sel = 1'b1;
      end
      else if (x <= 98304) begin seg_x0 = 65536; seg_x1 = 98304; seg_y0 = 64357; seg_y1 = 98061; lerp_sel = 1'b1; end
      else if (x <= 131072) begin seg_x0 = 98304; seg_x1 = 131072; seg_y0 = 98061; seg_y1 = 131028; lerp_sel = 1'b1; end
      else y = x;
    if (lerp_sel) y = lerp(x, seg_x0, seg_x1, seg_y0, seg_y1);
    y_out = y[DATA_W-1:0];
  end
endmodule
