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
  function automatic logic signed [63:0] router_lerp(
    input logic signed [63:0] xv,
    input logic signed [63:0] x0,
    input logic signed [63:0] x1,
    input logic signed [63:0] y0,
    input logic signed [63:0] y1
  );
    router_lerp = y0 + ((xv - x0) * (y1 - y0)) / (x1 - x0);
  endfunction

  function automatic logic [FRAC_W:0] router_sigmoid(
    input logic signed [DATA_W-1:0] x_in
  );
    logic signed [63:0] x, ax, pos, y;
    localparam logic signed [63:0] ONE = 64'sd1 <<< FRAC_W;
    begin
      x = x_in;
      ax = (x < 0) ? -x : x;
      if (ax <= ONE/4)          pos = router_lerp(ax, 0, ONE/4, 8192, 9211);
      else if (ax <= ONE/2)     pos = router_lerp(ax, ONE/4, ONE/2, 9211, 10198);
      else if (ax <= 3*ONE/4)   pos = router_lerp(ax, ONE/2, 3*ONE/4, 10198, 11128);
      else if (ax <= ONE)       pos = router_lerp(ax, 3*ONE/4, ONE, 11128, 11978);
      else if (ax <= 5*ONE/4)   pos = router_lerp(ax, ONE, 5*ONE/4, 11978, 12735);
      else if (ax <= 3*ONE/2)   pos = router_lerp(ax, 5*ONE/4, 3*ONE/2, 12735, 13395);
      else if (ax <= 7*ONE/4)   pos = router_lerp(ax, 3*ONE/2, 7*ONE/4, 13395, 13958);
      else if (ax <= 2*ONE)     pos = router_lerp(ax, 7*ONE/4, 2*ONE, 13958, 14431);
      else if (ax <= 4*ONE)     pos = router_lerp(ax, 2*ONE, 4*ONE, 14431, 16089);
      else if (ax <= 8*ONE)     pos = router_lerp(ax, 4*ONE, 8*ONE, 16089, 16379);
      else                      pos = ONE;
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
