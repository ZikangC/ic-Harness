// One q/k/v stream's causal depthwise convolution.  It deliberately stores
// pre-convolution samples, matching the architecture's recurrence.
module causal_conv4 #(
  parameter int CHANNELS = 64,
  parameter int DATA_W   = 24,
  parameter int FRAC_W   = 14
) (
  input  logic                              clk,
  input  logic                              rst_n,
  input  logic                              in_valid,
  input  logic signed [CHANNELS*DATA_W-1:0] in_bus,
  input  logic signed [CHANNELS*4*16-1:0]   taps_bus, // each tap is Q4.11
  output logic signed [CHANNELS*DATA_W-1:0] pre_silu_bus
);
  logic signed [DATA_W-1:0] history [0:2][0:CHANNELS-1];
  integer channel_comb, tap_comb;
  integer channel_seq, tap_seq;
  logic signed [63:0] sum;
  logic signed [DATA_W-1:0] sample;
  logic signed [15:0] coefficient;

  always_comb begin
    pre_silu_bus = '0;
    for (channel_comb = 0; channel_comb < CHANNELS; channel_comb = channel_comb + 1) begin
      sum = 0;
      for (tap_comb = 0; tap_comb < 4; tap_comb = tap_comb + 1) begin
        if (tap_comb < 3)
          sample = history[tap_comb][channel_comb];
        else
          sample = $signed(in_bus[channel_comb*DATA_W +: DATA_W]);
        coefficient = $signed(taps_bus[(channel_comb*4 + tap_comb)*16 +: 16]);
        sum = sum + sample * coefficient;
      end
      // data is Q*.14 and coefficients Q4.11, so shift by 11.
      pre_silu_bus[channel_comb*DATA_W +: DATA_W] = sum >>> 11;
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (tap_seq = 0; tap_seq < 3; tap_seq = tap_seq + 1)
        for (channel_seq = 0; channel_seq < CHANNELS; channel_seq = channel_seq + 1)
          history[tap_seq][channel_seq] <= '0;
    end else if (in_valid) begin
      for (channel_seq = 0; channel_seq < CHANNELS; channel_seq = channel_seq + 1) begin
        history[0][channel_seq] <= history[1][channel_seq];
        history[1][channel_seq] <= history[2][channel_seq];
        history[2][channel_seq] <= in_bus[channel_seq*DATA_W +: DATA_W];
      end
    end
  end
endmodule
