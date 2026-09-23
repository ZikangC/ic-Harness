// One MLA head's causal attention.  It scans cache positions 0..valid_count-1
// three times: q.K scores, then a sequential max-subtracted softmax
// (max -> exp-PWL accumulation -> reciprocal multiply), then softmax-weighted
// V accumulation.
//
// The softmax is computed one element per cycle with a single shared exp-PWL
// unit and a single reciprocal divider, reusing the already-filled score_mem.
// The previous 64-wide combinational softmax (64 parallel 16-branch PWL lerp
// chains, each with 64-bit divisions, instanced twice by head0/head1) grew to
// 3.09E+07 cells after techmap and stalled ABC at any budget; the sequential
// unit keeps the same arithmetic bit-exact and maps in seconds.
module mla_attention_head #(
  parameter int MAX_SEQ = 64,
  parameter int KD      = 48,
  parameter int DV      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14,
  localparam int POS_W  = $clog2(MAX_SEQ)
) (
  input  logic                        clk,
  input  logic                        rst_n,
  input  logic                        start,
  input  logic [POS_W:0]              valid_count,
  input  logic signed [KD*DATA_W-1:0] query_bus,
  output logic [POS_W-1:0]            cache_index,
  input  logic signed [KD*DATA_W-1:0] cache_key_bus,
  input  logic signed [DV*DATA_W-1:0] cache_value_bus,
  output logic                        busy,
  output logic                        done,
  output logic signed [DV*DATA_W-1:0] context_bus
);
  typedef enum logic [2:0] {IDLE, SCORE, SMAX, SSUM, SDIV, VALUE, FINISH} state_t;
  state_t state;
  logic [POS_W-1:0] index;
  logic signed [DATA_W-1:0] score_mem [0:MAX_SEQ-1];
  logic [FRAC_W:0] exp_mem [0:MAX_SEQ-1];
  logic [FRAC_W:0] prob_mem [0:MAX_SEQ-1];
  logic signed [63:0] value_accum [0:DV-1];
  logic signed [63:0] dot;
  logic signed [DATA_W-1:0] q_value, k_value, v_value;
  // Exact-width product intermediates: computing q*k / v*prob directly in the
  // 64-bit context of `dot`/`value_accum` widens the 24x24/24x16 multipliers
  // into 64x64 arrays (the ~1.13M-cell pre-ABC bulk).  Sized to the exact
  // product width (24+24=48, 24+16=40) the products are bit-identical and map
  // to narrow multipliers; accumulation into 64-bit accumulators is unchanged.
  logic signed [2*DATA_W-1:0] qk_prod;
  logic signed [DATA_W+FRAC_W+1:0] vp_prod;
  logic [FRAC_W:0] exp_value;
  logic [FRAC_W:0] probability;
  logic signed [63:0] max_score, delta, sum_exp, prob_result;
  integer d, p;

  localparam logic signed [63:0] ONE = 64'sd1 <<< FRAC_W;

  // Division-free lerp, narrowed from 64-bit to 32-bit signed (same pattern
  // as the golden-verified sigmoid_pwl/silu_pwl lerp).  Every segment width
  // x1-x0 below is a power of two (2^15/2^14/2^13/2^12), so the runtime
  // divide is replaced by an arithmetic shift by k=log2(x1-x0).  In each
  // exp_negative branch dx = x-x0 is in (0,2^k] and dy = y1-y0 > 0, so the
  // dividend dx*dy is non-negative; the rounding-bias term reproduces
  // truncation-toward-zero signed division bit-exactly in general (and is
  // provably inactive here).  Max |dx| = 32768 and max |dy| = 3624 give
  // |p| < 2^27 < 2^31, so the 32x32 multiply holds the former 64-bit
  // numerator exactly and the quotient is bit-identical.
  function automatic logic signed [63:0] lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    logic signed [31:0] dx, dy, p, w, bias, q;
    logic [4:0] k;
    w    = 32'(x1 - x0);            // 2^k, k in {12,13,14,15}
    k    = (w == 4096)  ? 5'd12 :
           (w == 8192)  ? 5'd13 :
           (w == 16384) ? 5'd14 : 5'd15;
    dx   = 32'(x_value - x0);       // (0,32768] when a segment is selected
    dy   = 32'(y1 - y0);            // [36,3624], always positive here
    p    = dx * dy;                 // |p| < 2^27 < 2^31, no overflow
    bias = p[31] ? (w - 32'sd1) : 32'sd0;
    q    = (p + bias) >>> k;
    lerp = y0 + 64'(q);
  endfunction

  // exp(-x) PWL, bit-identical to the removed softmax_vector_pwl unit:
  // exact at each sample in Q1.14, sampled every 0.25 over the critical
  // [-2,0] span and more densely below.
  function automatic logic [FRAC_W:0] exp_negative(input logic signed [63:0] x);
    logic signed [63:0] y;
    begin
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
    busy = (state != IDLE) && (state != FINISH);
    done = (state == FINISH);
    cache_index = index;
    context_bus = '0;
    for (d = 0; d < DV; d = d + 1)
      context_bus[d*DATA_W +: DATA_W] = value_accum[d] >>> FRAC_W;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      index <= '0;
      max_score <= '0;
      sum_exp <= '0;
      for (p = 0; p < MAX_SEQ; p = p + 1) begin
        score_mem[p] <= '0;
        exp_mem[p] <= '0;
        prob_mem[p] <= '0;
      end
      for (d = 0; d < DV; d = d + 1) value_accum[d] <= '0;
    end else begin
      case (state)
        IDLE: if (start) begin
          index <= '0;
          max_score <= -(64'sd1 <<< (DATA_W - 1));
          sum_exp <= '0;
          for (d = 0; d < DV; d = d + 1) value_accum[d] <= '0;
          state <= SCORE;
        end
        SCORE: begin
          dot = 0;
          // Accumulate the full-precision Q2*FRAC products and rescale once.
          // The previous per-term `(q*k) >>> FRAC_W` truncation biased every
          // attention logit downward by up to 0.5 LSB; over 48 key dims this
          // systematic bias shifts the softmax probabilities and, through the
          // MLA context, the L1 router near-ties.  The single 29-bit shift
          // below produces the same Q*.14 score scale as before.
          for (d = 0; d < KD; d = d + 1) begin
            q_value = $signed(query_bus[d*DATA_W +: DATA_W]);
            k_value = $signed(cache_key_bus[d*DATA_W +: DATA_W]);
            qk_prod = q_value * k_value;
            dot = dot + qk_prod;
          end
          // 1/sqrt(48) ~= 4729 / 2^15; rescale the Q2*FRAC dot to Q*.14.
          score_mem[index] <= (dot * 4729) >>> (FRAC_W + 15);
          if (index + 1 >= valid_count) begin
            index <= '0;
            state <= SMAX;
          end
          else index <= index + 1'b1;
        end
        // Pass 1: running max over the filled score_mem (first-wins, same
        // strict-`>` ordering as the combinational reference).
        SMAX: begin
          if (index < valid_count) begin
            if (score_mem[index] > max_score)
              max_score <= $signed(score_mem[index]);
          end
          if (index + 1 >= valid_count) begin
            index <= '0;
            state <= SSUM;
          end
          else index <= index + 1'b1;
        end
        // Pass 2: delta = score - max; exp-PWL once per element; accumulate
        // sum_exp and cache exp values for the reciprocal pass.
        SSUM: begin
          if (index < valid_count) begin
            delta = $signed(score_mem[index]) - max_score;
            exp_value = exp_negative(delta);
            exp_mem[index] <= exp_value;
            sum_exp <= sum_exp + exp_value;
          end
          if (index + 1 >= valid_count) begin
            index <= '0;
            state <= SDIV;
          end
          else index <= index + 1'b1;
        end
        // Pass 3: probability = (exp<<14) / sum_exp with the same truncation
        // and the same `sum_exp != 0` guard as the combinational reference.
        SDIV: begin
          if (index < valid_count && sum_exp != 0) begin
            prob_result = ($signed({1'b0, exp_mem[index]}) <<< FRAC_W) / sum_exp;
            prob_mem[index] <= prob_result[FRAC_W:0];
          end
          else if (index < valid_count) begin
            prob_mem[index] <= '0;
          end
          if (index + 1 >= valid_count) begin
            index <= '0;
            state <= VALUE;
          end
          else index <= index + 1'b1;
        end
        VALUE: begin
          probability = prob_mem[index];
          for (d = 0; d < DV; d = d + 1) begin
            v_value = $signed(cache_value_bus[d*DATA_W +: DATA_W]);
            vp_prod = v_value * $signed({1'b0, probability});
            value_accum[d] <= value_accum[d] + vp_prod;
          end
          if (index + 1 >= valid_count) state <= FINISH;
          else index <= index + 1'b1;
        end
        FINISH: state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end
endmodule
