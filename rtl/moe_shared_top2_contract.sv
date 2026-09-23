// Combines the always-on shared SwiGLU result with two router-selected expert
// results. Router bias is used solely for stable selection; normalized raw
// sigmoid scores provide the 2.828-scaled mixture weights.
module moe_shared_top2_contract #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic signed [8*DATA_W-1:0] router_pre_bus,
  input logic signed [8*DATA_W-1:0] bias_bus,
  input logic signed [64*DATA_W-1:0] shared_bus, expert0_bus, expert1_bus,
  output logic [2:0] expert0, expert1,
  output logic signed [DATA_W-1:0] weight0, weight1,
  output logic signed [64*DATA_W-1:0] out_bus
);
  logic signed [8*DATA_W-1:0] scores;
  logic [8*(FRAC_W+1)-1:0] sigmoid_scores;
  logic signed [63:0] sum;
  integer i;

  // Router ranking is a discontinuous boundary: the shared coarse sigmoid
  // (one segment over [0, 2]) can reverse close score+bias pairs even when
  // the incoming norm vector is otherwise close to reference.  Keep this
  // accuracy improvement local to routing so KDA gates and SwiGLU retain
  // their independently calibrated approximation.  Values are sigmoid(x)
  // rounded to Q1.14 at quarter steps over [0, 2], with symmetric negative
  // handling and the original coarser tails.
  // Division-free lerp, narrowed from 64-bit to 32-bit signed.  Every segment
  // width below is a power of two (x1-x0 = 4096/32768/65536 = 2^12/2^15/2^16)
  // and the dividend dx*dy is non-negative in every branch (ax>=x0, y1>y0),
  // so truncating integer division equals an arithmetic right shift by
  // k=log2(x1-x0).  The largest product is 32768*1658 < 2^26, far inside
  // 32-bit signed, so the narrow multiply is bit-exact to the former 64-bit
  // divide and no rounding bias is needed (p is never negative).
  function automatic logic signed [31:0] router_lerp(
    input logic signed [31:0] xv,
    input logic signed [31:0] x0,
    input logic signed [31:0] x1,
    input logic signed [31:0] y0,
    input logic signed [31:0] y1
  );
    logic signed [31:0] dx, dy, p, q, w;
    logic [4:0] k;
    w  = x1 - x0;
    k  = (w == 32'sd4096)  ? 5'd12 :
         (w == 32'sd32768) ? 5'd15 : 5'd16;
    dx = xv - x0;
    dy = y1 - y0;
    p  = dx * dy;
    q  = p >>> k;
    router_lerp = y0 + q;
  endfunction

  function automatic logic [FRAC_W:0] router_sigmoid(
    input logic signed [DATA_W-1:0] x_in
  );
    logic signed [31:0] x, ax, pos, y;
    logic signed [31:0] x0, x1, y0, y1;
    localparam logic signed [31:0] ONE = 32'sd1 <<< FRAC_W;
    begin
      x = x_in;
      ax = (x < 0) ? -x : x;
      // Segment mux first, then one shared lerp.  Same boundaries, same
      // interpolation constants and identical lerp/k logic, so the result is
      // bit-exact to the former per-branch lerp; the 32x32 multiplier count
      // per sigmoid drops from 10 to 1.  Tail (ax > 8*ONE) pins pos = ONE and
      // the lerp result is discarded there.
      if (ax <= ONE/4)        begin x0 = 0;         x1 = ONE/4;   y0 = 8192;  y1 = 9211;  end
      else if (ax <= ONE/2)   begin x0 = ONE/4;     x1 = ONE/2;   y0 = 9211;  y1 = 10198; end
      else if (ax <= 3*ONE/4) begin x0 = ONE/2;     x1 = 3*ONE/4; y0 = 10198; y1 = 11128; end
      else if (ax <= ONE)     begin x0 = 3*ONE/4;   x1 = ONE;     y0 = 11128; y1 = 11978; end
      else if (ax <= 5*ONE/4) begin x0 = ONE;       x1 = 5*ONE/4; y0 = 11978; y1 = 12735; end
      else if (ax <= 3*ONE/2) begin x0 = 5*ONE/4;   x1 = 3*ONE/2; y0 = 12735; y1 = 13395; end
      else if (ax <= 7*ONE/4) begin x0 = 3*ONE/2;   x1 = 7*ONE/4; y0 = 13395; y1 = 13958; end
      else if (ax <= 2*ONE)   begin x0 = 7*ONE/4;   x1 = 2*ONE;   y0 = 13958; y1 = 14431; end
      else if (ax <= 4*ONE)   begin x0 = 2*ONE;     x1 = 4*ONE;   y0 = 14431; y1 = 16089; end
      else if (ax <= 8*ONE)   begin x0 = 4*ONE;     x1 = 8*ONE;   y0 = 16089; y1 = 16379; end
      else                    begin x0 = 8*ONE;     x1 = 8*ONE;   y0 = 0;     y1 = 0;     end
      if (ax <= 8*ONE)
        pos = router_lerp(ax, x0, x1, y0, y1);
      else
        pos = ONE;
      y = (x < 0) ? (ONE - pos) : pos;
      router_sigmoid = y[FRAC_W:0];
    end
  endfunction

  always_comb begin
    for (i = 0; i < 8; i = i + 1)
      sigmoid_scores[i*(FRAC_W+1) +: FRAC_W+1] = router_sigmoid(router_pre_bus[i*DATA_W +: DATA_W]);
  end
  always_comb begin
    for (i = 0; i < 8; i = i + 1) scores[i*DATA_W +: DATA_W] = $signed({1'b0, sigmoid_scores[i*(FRAC_W+1) +: FRAC_W+1]});
  end
  moe_top2_router #(.EXPERTS(8), .SCORE_W(DATA_W), .FRAC_W(FRAC_W)) route
    (.score_bus(scores), .bias_bus, .expert0, .expert1, .weight0, .weight1);
  always_comb begin
    out_bus = '0;
    for (i = 0; i < 64; i = i + 1) begin
      sum = $signed(shared_bus[i*DATA_W +: DATA_W]) +
            (($signed(expert0_bus[i*DATA_W +: DATA_W]) * weight0) >>> FRAC_W) +
            (($signed(expert1_bus[i*DATA_W +: DATA_W]) * weight1) >>> FRAC_W);
      out_bus[i*DATA_W +: DATA_W] = sum;
    end
  end
endmodule
