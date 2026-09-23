// One column of a 32-dimensional KDA head.  kda_delta_head's original single
// always_comb computed all 32 columns together, flattening the whole 32x32
// MAC array (~4096 24x24 multipliers) into ONE module whose Yosys techmap
// netlist exploded to ~19M cells and never reached ABC (round-16/21 evidence).
// The column loop has NO cross-column data dependency -- each column re-zeroes
// its own `sum`, decay uses per-row alpha (alpha[row]), and the delta and
// readout steps only touch that column's own state column -- so the computation
// is bit-identical per column and is moved verbatim into this parameterized
// single-column module.  kda_delta_head now instantiates DH of these columns,
// letting the measure flow map the single-column body (pass 1, leaves first)
// in a bounded ABC window and the head's own wrapper glue (pass 2, top last)
// separately.
//
// Every expression is copied from the original kda_delta_head always_comb:
// the 64-bit sum accumulation order (row 0..DH-1), mul_q14, the 24-bit
// prediction/error truncation, the delta rounding shift, and the 5793 rescale
// with its rounding shift.  Ports, latency and the FSM contract are unchanged.
module kda_delta_col #(
  parameter int DH      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14
) (
  input  logic                          clk,
  input  logic                          rst_n,
  input  logic                          step_valid,
  input  logic signed [DH*DATA_W-1:0]   q_in,
  input  logic signed [DH*DATA_W-1:0]   k_in,
  input  logic signed [DATA_W-1:0]      v_in,
  input  logic signed [DH*DATA_W-1:0]   alpha_in,
  input  logic signed [DATA_W-1:0]      beta_in,
  output logic signed [DATA_W-1:0]      out_bus
);
  // The KDA state and the recurrence readout are carried four fractional bits
  // wider (Q*.18) than the external Q*.14 datapath.  After alpha decay the
  // state magnitude is tiny, so a Q14 readout quantizes the low-magnitude
  // head_raw to a handful of LSBs; the onorm RMSNorm then amplifies that
  // quantization error.  Keeping the state and readout at Q*.18 removes the
  // dominant error source before head_norm is gated back to Q*.14.
  localparam int STATE_FRAC_W = FRAC_W + 4;
  logic signed [DATA_W-1:0] state      [0:DH-1];
  logic signed [DATA_W-1:0] decayed    [0:DH-1];
  logic signed [DATA_W-1:0] next_state [0:DH-1];
  logic signed [63:0] prediction;
  logic signed [DATA_W-1:0] error;
  integer row_comb;
  integer row_seq;
  logic signed [DATA_W-1:0] alpha_value, key_value, query_value, value_value;
  logic signed [63:0] sum;
  logic signed [63:0] product;
  logic signed [63:0] delta_product;

  // Keep the product wide until its Q14 rescale.  A direct assignment such
  // as `(a * b) >>> FRAC_W` is context-sized to DATA_W by several tools;
  // with Q14 operands it silently truncated beta*k before the shift.
  function automatic logic signed [DATA_W-1:0] mul_q14(
      input logic signed [DATA_W-1:0] a,
      input logic signed [DATA_W-1:0] b
  );
    logic signed [63:0] wide_product;
    begin
      wide_product = a * b;
      mul_q14 = wide_product >>> FRAC_W;
    end
  endfunction

  always_comb begin
    // 1. decay old state along its key/channel rows.
    for (row_comb = 0; row_comb < DH; row_comb = row_comb + 1) begin
      alpha_value = $signed(alpha_in[row_comb*DATA_W +: DATA_W]);
      decayed[row_comb] = mul_q14($signed(state[row_comb]), alpha_value);
    end

    // 2. predict v from the decayed state: k @ S.  Full-precision products
    //    accumulate in the 64-bit sum and are shifted exactly once; the
    //    previous per-term `(product >>> FRAC_W)` truncation biased the
    //    low-magnitude recurrence readout that RMSNorm later amplifies.
    sum = 0;
    for (row_comb = 0; row_comb < DH; row_comb = row_comb + 1) begin
      key_value = $signed(k_in[row_comb*DATA_W +: DATA_W]);
      product = key_value * $signed(decayed[row_comb]);
      sum = sum + product;
    end
    prediction = sum >>> STATE_FRAC_W;
    value_value = $signed(v_in);
    error = value_value - prediction;

    // 3. delta write: S += outer(beta*k, v-kS).  beta*k stays wide (Q2*FRAC);
    //    one final shift restores the Q*.18 delta, avoiding the double
    //    truncation of the previous beta_key -> mul_q14 chain.  The delta is
    //    rounded (half-LSB added before the arithmetic shift) rather than
    //    truncated; the Q18 state values are tiny after alpha decay, so a
    //    consistent truncation bias per token accumulated into a low-magnitude
    //    readout that the onorm RMSNorm then amplified.
    for (row_comb = 0; row_comb < DH; row_comb = row_comb + 1) begin
      key_value = $signed(k_in[row_comb*DATA_W +: DATA_W]);
      delta_product = key_value * $signed(beta_in);
      delta_product = delta_product * $signed(error);
      next_state[row_comb] =
        decayed[row_comb] + ((delta_product + (64'sd1 << (3*FRAC_W - STATE_FRAC_W - 1))) >>> (3*FRAC_W - STATE_FRAC_W));
    end

    // 4. read from the UPDATED state: q @ S / sqrt(DH).  Full-precision
    //    accumulate, single rescale by 5793/2^(FRAC_W+15), rounded at the
    //    final shift.  The state is Q*.18, so the q@S sum is Q2*(FRAC_W+4)
    //    and the result is a Q*.18 head_raw; the caller's onorm consumes
    //    this wider readout before gating back to Q*.14.
    out_bus = '0;
    sum = 0;
    for (row_comb = 0; row_comb < DH; row_comb = row_comb + 1) begin
      query_value = $signed(q_in[row_comb*DATA_W +: DATA_W]);
      product = query_value * $signed(next_state[row_comb]);
      sum = sum + product;
    end
    // DH=32; /sqrt(32) is approximated by multiplication with 5793/32768.
    product = sum * 5793;
    out_bus = (product + (64'sd1 << (FRAC_W + 14))) >>> (FRAC_W + 15);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (row_seq = 0; row_seq < DH; row_seq = row_seq + 1)
        state[row_seq] <= '0;
    end else if (step_valid) begin
      for (row_seq = 0; row_seq < DH; row_seq = row_seq + 1)
        state[row_seq] <= next_state[row_seq];
    end
  end
endmodule
