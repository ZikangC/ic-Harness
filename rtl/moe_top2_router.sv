// Stable top-2 router. Scores must be sigmoid outputs in non-negative Q*.14;
// bias only affects selection. The output weights use raw scores and sum to
// ROUTER_SCALE=2.828, represented in Q*.14 as 46334.
module moe_top2_router #(
  parameter int EXPERTS = 8,
  parameter int SCORE_W = 24,
  parameter int FRAC_W  = 14
) (
  input  logic signed [EXPERTS*SCORE_W-1:0] score_bus,
  input  logic signed [EXPERTS*SCORE_W-1:0] bias_bus,
  output logic [$clog2(EXPERTS)-1:0]        expert0,
  output logic [$clog2(EXPERTS)-1:0]        expert1,
  output logic signed [SCORE_W-1:0]         weight0,
  output logic signed [SCORE_W-1:0]         weight1
);
  integer i;
  logic signed [SCORE_W-1:0] score_i, select_i;
  logic signed [SCORE_W-1:0] best0, best1, raw0, raw1;
  logic signed [SCORE_W:0] denominator;
  logic signed [63:0] scaled;

  always_comb begin
    best0 = -((1 <<< (SCORE_W-1)) - 1);
    best1 = -((1 <<< (SCORE_W-1)) - 1);
    raw0 = '0;
    raw1 = '0;
    expert0 = '0;
    expert1 = '0;
    weight0 = '0;
    weight1 = '0;
    scaled = '0;
    // Strict > preserves the earlier (smaller) index on ties.
    for (i = 0; i < EXPERTS; i = i + 1) begin
      score_i = $signed(score_bus[i*SCORE_W +: SCORE_W]);
      select_i = score_i + $signed(bias_bus[i*SCORE_W +: SCORE_W]);
      if (select_i > best0) begin
        best1 = best0; raw1 = raw0; expert1 = expert0;
        best0 = select_i; raw0 = score_i; expert0 = i[$clog2(EXPERTS)-1:0];
      end else if (select_i > best1) begin
        best1 = select_i; raw1 = score_i; expert1 = i[$clog2(EXPERTS)-1:0];
      end
    end
    denominator = raw0 + raw1;
    if (denominator > 0) begin
      scaled = raw0 * 46334; weight0 = scaled / denominator;
      scaled = raw1 * 46334; weight1 = scaled / denominator;
    end else begin
      weight0 = '0;
      weight1 = '0;
    end
  end
endmodule
