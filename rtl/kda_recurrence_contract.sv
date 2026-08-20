// KDA recurrence after the six q4 projections have been materialized.
// It owns three pre-conv histories, L2 q/k normalization, b/f/A/dtb decay,
// state update/read, onorm and the g1/g2 sigmoid gate. The caller applies the
// final q4 `L*.kda.o` projection to `core_bus`.
module kda_recurrence_contract #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic clk, input logic rst_n, input logic step_valid,
  input logic signed [64*DATA_W-1:0] q_pre_bus, k_pre_bus, v_pre_bus,
  input logic signed [64*4*16-1:0] qconv_bus, kconv_bus, vconv_bus,
  input logic signed [2*DATA_W-1:0] b_bus,
  input logic signed [64*DATA_W-1:0] f_bus, z_bus,
  input logic [2*16-1:0] A_bus,                    // Q8.8 unsigned
  input logic signed [64*16-1:0] dtb_bus,          // Q4.11
  input logic signed [32*16-1:0] onorm_offset_bus, // Q2.13, shared per head
  output logic signed [64*DATA_W-1:0] core_bus
);
  localparam int DH = 32;
  logic signed [64*DATA_W-1:0] q_conv, k_conv, v_conv, q_act, k_act, v_act;
  logic signed [64*DATA_W-1:0] q_norm, k_norm, alpha_bus, head_raw, head_norm;
  logic [64*(FRAC_W+1)-1:0] z_gate;
  logic [2*(FRAC_W+1)-1:0] beta_gate;
  logic signed [2*DATA_W-1:0] beta_bus;
  integer i, h;

  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) qconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(q_pre_bus), .taps_bus(qconv_bus), .pre_silu_bus(q_conv));
  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) kconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(k_pre_bus), .taps_bus(kconv_bus), .pre_silu_bus(k_conv));
  causal_conv4 #(.CHANNELS(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) vconv
    (.clk, .rst_n, .in_valid(step_valid), .in_bus(v_pre_bus), .taps_bus(vconv_bus), .pre_silu_bus(v_conv));
  generate
    genvar lane;
    for (lane = 0; lane < 64; lane = lane + 1) begin : nonlinear
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sq (.x_in(q_conv[lane*DATA_W +: DATA_W]), .y_out(q_act[lane*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sk (.x_in(k_conv[lane*DATA_W +: DATA_W]), .y_out(k_act[lane*DATA_W +: DATA_W]));
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sv (.x_in(v_conv[lane*DATA_W +: DATA_W]), .y_out(v_act[lane*DATA_W +: DATA_W]));
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sz (.x_in(z_bus[lane*DATA_W +: DATA_W]), .y_out(z_gate[lane*(FRAC_W+1) +: FRAC_W+1]));
    end
    for (lane = 0; lane < 2; lane = lane + 1) begin : beta_nonlinear
      sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sb (.x_in(b_bus[lane*DATA_W +: DATA_W]), .y_out(beta_gate[lane*(FRAC_W+1) +: FRAC_W+1]));
      l2norm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) nq
        (.x_bus(q_act[lane*DH*DATA_W +: DH*DATA_W]), .y_bus(q_norm[lane*DH*DATA_W +: DH*DATA_W]));
      l2norm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) nk
        (.x_bus(k_act[lane*DH*DATA_W +: DH*DATA_W]), .y_bus(k_norm[lane*DH*DATA_W +: DH*DATA_W]));
      kda_delta_head #(.DH(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) delta_state
        (.clk, .rst_n, .step_valid, .q_in(q_norm[lane*DH*DATA_W +: DH*DATA_W]),
         .k_in(k_norm[lane*DH*DATA_W +: DH*DATA_W]), .v_in(v_act[lane*DH*DATA_W +: DH*DATA_W]),
         .alpha_in(alpha_bus[lane*DH*DATA_W +: DH*DATA_W]), .beta_in(beta_bus[lane*DATA_W +: DATA_W]),
         .out_bus(head_raw[lane*DH*DATA_W +: DH*DATA_W]));
      rmsnorm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) no
        (.x_bus(head_raw[lane*DH*DATA_W +: DH*DATA_W]), .norm_offset_bus(onorm_offset_bus),
         .y_bus(head_norm[lane*DH*DATA_W +: DH*DATA_W]));
    end
  endgenerate

  // PWL interpolation for exp(-5*s), s in unsigned Q1.14.  It is monotonic
  // and bounded, while retaining the exact KDA gate data dependency.
  function automatic logic signed [DATA_W-1:0] decay_from_sigmoid(input logic [FRAC_W:0] s);
    logic signed [63:0] y;
    begin
      if (s <= 4096) y = 16384 - (s * 11690) / 4096;
      else if (s <= 8192) y = 4694 - ((s-4096) * 3349) / 4096;
      else if (s <= 12288) y = 1345 - ((s-8192) * 959) / 4096;
      else y = 386 - ((s-12288) * 276) / 4096;
      if (y < 110) y = 110;
      decay_from_sigmoid = y;
    end
  endfunction

  // Match sigmoid_pwl exactly on the alpha path; the former single straight
  // line around zero distorted f/dtb decay substantially before exp PWL.
  function automatic logic [FRAC_W:0] sigmoid_from_q14(input logic signed [63:0] x);
    logic signed [63:0] y;
    begin
      if (x <= -(8 <<< FRAC_W)) y = 9;
      else if (x <= -(4 <<< FRAC_W)) y = 9 + ((x + (8 <<< FRAC_W)) * (295-9)) / (4 <<< FRAC_W);
      else if (x <= -(2 <<< FRAC_W)) y = 295 + ((x + (4 <<< FRAC_W)) * (1953-295)) / (2 <<< FRAC_W);
      else if (x <= 0) y = 1953 + ((x + (2 <<< FRAC_W)) * (8192-1953)) / (2 <<< FRAC_W);
      else if (x <= (2 <<< FRAC_W)) y = 8192 + (x * (14431-8192)) / (2 <<< FRAC_W);
      else if (x <= (4 <<< FRAC_W)) y = 14431 + ((x - (2 <<< FRAC_W)) * (16089-14431)) / (2 <<< FRAC_W);
      else if (x <= (8 <<< FRAC_W)) y = 16089 + ((x - (4 <<< FRAC_W)) * (16375-16089)) / (4 <<< FRAC_W);
      else y = 16384;
      sigmoid_from_q14 = y[FRAC_W:0];
    end
  endfunction

  logic signed [DATA_W-1:0] gate_argument;
  logic [FRAC_W:0] gate_sigmoid;
  logic signed [63:0] alpha_product, output_product;
  always_comb begin
    alpha_bus = '0; beta_bus = '0;
    for (h = 0; h < 2; h = h + 1) begin
      beta_bus[h*DATA_W +: DATA_W] = $signed({1'b0, beta_gate[h*(FRAC_W+1) +: FRAC_W+1]});
      for (i = 0; i < DH; i = i + 1) begin
        alpha_product = $signed({1'b0, A_bus[h*16 +: 16]}) *
                  ($signed(f_bus[(h*DH+i)*DATA_W +: DATA_W]) + ($signed(dtb_bus[(h*DH+i)*16 +: 16]) <<< 3));
        gate_argument = alpha_product >>> 8;
        // Inline sigmoid permits the alpha path to remain combinational.
        gate_sigmoid = sigmoid_from_q14(gate_argument);
        alpha_bus[(h*DH+i)*DATA_W +: DATA_W] = decay_from_sigmoid(gate_sigmoid);
      end
    end
  end
  always_comb begin
    core_bus = '0;
    for (h = 0; h < 2; h = h + 1) begin
      for (i = 0; i < DH; i = i + 1) begin
        output_product = $signed(head_norm[(h*DH+i)*DATA_W +: DATA_W]) *
                  $signed({1'b0, z_gate[(h*DH+i)*(FRAC_W+1) +: FRAC_W+1]});
        core_bus[(h*DH+i)*DATA_W +: DATA_W] = output_product >>> FRAC_W;
      end
    end
  end
endmodule
