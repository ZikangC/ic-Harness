// Max-subtracted softmax for up to 64 scores. exp(delta), delta<=0, uses a
// bounded piecewise-linear approximation and returns probabilities in Q1.14.
// The module is a functional combinational baseline; a chip implementation
// should pipeline max, exp-LUT accumulation and reciprocal multiplication.
module softmax_vector_pwl #(
  parameter int MAX_ELEMS = 64,
  parameter int DATA_W    = 24,
  parameter int FRAC_W    = 14,
  localparam int COUNT_W  = $clog2(MAX_ELEMS + 1)
) (
  input  logic [COUNT_W-1:0]                 count,
  input  logic signed [MAX_ELEMS*DATA_W-1:0] score_bus,
  output logic        [MAX_ELEMS*(FRAC_W+1)-1:0] probability_bus
);
  localparam logic signed [63:0] ONE = 64'sd1 <<< FRAC_W;
  logic [FRAC_W:0] exp_value [0:MAX_ELEMS-1];
  logic signed [63:0] max_score;
  logic signed [63:0] score_value;
  logic signed [63:0] delta;
  logic signed [63:0] sum_exp;
  logic signed [63:0] probability;
  integer i;

  function automatic logic signed [63:0] lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    lerp = y0 + ((x_value - x0) * (y1 - y0)) / (x1 - x0);
  endfunction

  function automatic logic [FRAC_W:0] exp_negative(input logic signed [63:0] x);
    logic signed [63:0] y;
    begin
      // exp(-x) PWL, exact at each sample in Q1.14.  The previous five
      // breakpoints ([-8,-6,-4,-2,0]) left up to ~50% error near zero where
      // the MLA attention deltas concentrate; the exp curve is now sampled
      // every 0.25 over the critical [-2,0] span and more densely below.
      if (x <= -8*ONE)        y = 5;
      else if (x <= -6*ONE)   y = lerp(x, -8*ONE, -6*ONE, 5, 41);
      else if (x <= -5*ONE)   y = lerp(x, -6*ONE, -5*ONE, 41, 110);
      else if (x <= -4*ONE)   y = lerp(x, -5*ONE, -4*ONE, 110, 300);
      else if (x <= -3*ONE)   y = lerp(x, -4*ONE, -3*ONE, 300, 816);
      else if (x <= -5*ONE/2) y = lerp(x, -3*ONE, -5*ONE/2, 816, 1345);
      else if (x <= -2*ONE)   y = lerp(x, -5*ONE/2, -2*ONE, 1345, 2217);
      else if (x <= -7*ONE/4) y = lerp(x, -2*ONE, -7*ONE/4, 2217, 2847);
      else if (x <= -3*ONE/2) y = lerp(x, -7*ONE/4, -3*ONE/2, 2847, 3656);
      else if (x <= -5*ONE/4) y = lerp(x, -3*ONE/2, -5*ONE/4, 3656, 4694);
      else if (x <= -1*ONE)   y = lerp(x, -5*ONE/4, -1*ONE, 4694, 6027);
      else if (x <= -3*ONE/4) y = lerp(x, -1*ONE, -3*ONE/4, 6027, 7739);
      else if (x <= -1*ONE/2) y = lerp(x, -3*ONE/4, -1*ONE/2, 7739, 9937);
      else if (x <= -1*ONE/4) y = lerp(x, -1*ONE/2, -1*ONE/4, 9937, 12760);
      else if (x <= 0)        y = lerp(x, -1*ONE/4, 0, 12760, 16384);
      else                    y = 16384;
      exp_negative = y[FRAC_W:0];
    end
  endfunction

  always_comb begin
    score_value = '0;
    delta = '0;
    sum_exp = '0;
    probability = '0;
    probability_bus = '0;
    for (i = 0; i < MAX_ELEMS; i = i + 1)
      exp_value[i] = '0;

    max_score = -(64'sd1 <<< (DATA_W - 1));
    for (i = 0; i < MAX_ELEMS; i = i + 1) begin
      if (i < count) begin
        score_value = $signed(score_bus[i*DATA_W +: DATA_W]);
        if (score_value > max_score)
          max_score = score_value;
      end
    end

    sum_exp = 0;
    for (i = 0; i < MAX_ELEMS; i = i + 1) begin
      if (i < count) begin
        delta = $signed(score_bus[i*DATA_W +: DATA_W]) - max_score;
        exp_value[i] = exp_negative(delta);
        sum_exp = sum_exp + exp_value[i];
      end else begin
        exp_value[i] = '0;
      end
    end

    for (i = 0; i < MAX_ELEMS; i = i + 1) begin
      if (i < count && sum_exp != 0) begin
        probability = ($signed({1'b0, exp_value[i]}) <<< FRAC_W) / sum_exp;
        probability_bus[i*(FRAC_W+1) +: FRAC_W+1] = probability[FRAC_W:0];
      end
    end
  end
endmodule
