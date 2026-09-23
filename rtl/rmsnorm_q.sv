// Fixed-point RMSNorm for the architecture's 1-centered Q2.13 norm weights.
//
// x uses Q*.14.  The accumulated mean-square is therefore Q*.28; isqrt()
// returns rms in Q*.14.  The reciprocal has Q*.14, then each channel applies
// gain=(1+norm_offset), where norm_offset is the model's signed Q2.13 value.
// This is a combinational functional unit; a performance implementation would
// pipeline sum-of-squares, reciprocal-sqrt and channel scaling.
module rmsnorm_q #(
  parameter int DIM          = 64,
  parameter int DATA_W       = 24,
  parameter int FRAC_W       = 14,
  parameter int NORM_W       = 16,
  parameter int NORM_FRAC_W  = 13,
  // round(1e-5 * 2^(2*14)) = 2684
  parameter logic [63:0] EPS_Q2FRAC = 64'd2684
) (
  input  logic signed [DIM*DATA_W-1:0] x_bus,
  input  logic signed [DIM*NORM_W-1:0] norm_offset_bus,
  output logic signed [DIM*DATA_W-1:0] y_bus
);
  // Narrowed fixed-point containers.  Widths are exact, container-derived
  // bounds (no dependence on data or on EPS): every intermediate below holds
  // the same value the original 64-bit arithmetic produced, so the netlist
  // stays bit-identical while the dominant per-channel multipliers shrink
  // from ~24x64 / ~74x64 to 24xINV_W / NORMALIZED_W x GAIN_W.
  localparam int SUM_SQ_W     = 2*DATA_W + $clog2(DIM); // sum of DIM squares
  localparam int MEAN_SQ_W    = 2*DATA_W;               // < 2^(2*DATA_W-1)
  localparam int ISQRT_OUT_W  = (MEAN_SQ_W + 1) / 2;    // integer-sqrt root bits
  localparam int INV_W        = 2*FRAC_W + 2;           // (1<<2*FRAC_W)/rms fits
  localparam int PROD_INV_W   = DATA_W + INV_W;         // exact x*inv product
  localparam int NORMALIZED_W = DATA_W + FRAC_W + 1;    // post-shift normalized
  localparam int GAIN_W       = NORM_W + 1;             // 2^13 + signed 16-bit offset
  localparam int PROD_NORM_W  = NORMALIZED_W + GAIN_W;  // exact norm*gain product
  localparam int RESULT_W     = DATA_W + FRAC_W + NORM_W - NORM_FRAC_W;
  // Constants that were formerly parameter-width signed literals (INV_W'sd1,
  // GAIN_W'sd1); Verilator 5.040 rejects sized literals with parameter widths.
  localparam logic signed [INV_W-1:0]  INV_NUMERATOR = 1 <<< (2*FRAC_W); // 2^28, Q*.14 reciprocal numerator
  localparam logic signed [GAIN_W-1:0] GAIN_BIAS     = 1 <<< NORM_FRAC_W; // 2^13, Q2.13 gain bias

  function automatic logic signed [DATA_W-1:0] sat_data(
      input logic signed [63:0] value
  );
    logic signed [63:0] max_value;
    logic signed [63:0] min_value;
    begin
      max_value = (64'sd1 <<< (DATA_W - 1)) - 1;
      min_value = -(64'sd1 <<< (DATA_W - 1));
      if (value > max_value)
        sat_data = max_value[DATA_W-1:0];
      else if (value < min_value)
        sat_data = min_value[DATA_W-1:0];
      else
        sat_data = value[DATA_W-1:0];
    end
  endfunction

  function automatic logic [31:0] isqrt64(input logic [63:0] value);
    logic [63:0] root;
    logic [63:0] sq;  // invariant: sq == root * root
    integer bit_index;
    begin
      root = '0;
      sq = '0;
      // Incremental binary square root, bit-exact with the restoring form
      // (trial = root | (1<<k); keep iff trial*trial <= value).  For the old
      // root, trial*trial = sq + (root << (k+1)) + (1 << 2k), so after loop
      // unrolling each of the 32 stages is a 64-bit add + compare instead of
      // a 64x64 multiply; all quantities stay below 2^64 (no overflow).
      for (bit_index = 31; bit_index >= 0; bit_index = bit_index - 1) begin
        if ((sq + (root << (bit_index + 1)) + (64'd1 << (2 * bit_index))) <= value) begin
          sq = sq + (root << (bit_index + 1)) + (64'd1 << (2 * bit_index));
          root = root | (64'd1 << bit_index);
        end
      end
      isqrt64 = root[31:0];
    end
  endfunction

  integer i;
  logic signed [DATA_W-1:0] x_value;
  logic signed [NORM_W-1:0] offset_value;
  logic signed [SUM_SQ_W-1:0] sum_square;
  logic signed [SUM_SQ_W-1:0] mean_square_div;
  logic [MEAN_SQ_W-1:0] mean_square;
  logic [31:0] isqrt_result;
  logic [ISQRT_OUT_W-1:0] rms_qfrac;
  logic signed [INV_W-1:0] inv_rms_qfrac;
  logic signed [PROD_INV_W-1:0] x_mul_inv;
  logic signed [PROD_INV_W-1:0] x_mul_inv_shift;
  logic signed [NORMALIZED_W-1:0] normalized;
  logic signed [GAIN_W-1:0] gain_q13;
  logic signed [PROD_NORM_W-1:0] norm_mul_gain;
  logic signed [PROD_NORM_W-1:0] norm_mul_gain_shift;
  logic signed [RESULT_W-1:0] result;

  always_comb begin
    sum_square = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      sum_square = sum_square + x_value * x_value;
    end
    mean_square_div = sum_square / DIM;
    mean_square = mean_square_div[MEAN_SQ_W-1:0] + EPS_Q2FRAC[MEAN_SQ_W-1:0];
    isqrt_result = isqrt64(mean_square);
    rms_qfrac = isqrt_result[ISQRT_OUT_W-1:0];
    // (1<<(2*FRAC_W))/rms_q14 produces a reciprocal in Q*.14.
    if (rms_qfrac != 0)
      inv_rms_qfrac = INV_NUMERATOR / $signed({1'b0, rms_qfrac});
    else
      inv_rms_qfrac = '0;

    y_bus = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      offset_value = $signed(norm_offset_bus[i*NORM_W +: NORM_W]);
      // Full-width products first, then shift and truncate: keeps every
      // intermediate exactly equal to the original 64-bit datapath.
      x_mul_inv = x_value * inv_rms_qfrac;
      x_mul_inv_shift = x_mul_inv >>> FRAC_W;
      normalized = x_mul_inv_shift[NORMALIZED_W-1:0];
      gain_q13 = GAIN_BIAS + offset_value;
      norm_mul_gain = normalized * gain_q13;
      norm_mul_gain_shift = norm_mul_gain >>> NORM_FRAC_W;
      result = norm_mul_gain_shift[RESULT_W-1:0];
      y_bus[i*DATA_W +: DATA_W] = sat_data(result);
    end
  end
endmodule
