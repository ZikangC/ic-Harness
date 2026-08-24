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
      rmsnorm_q #(.DIM(DH), .DATA_W(DATA_W), .FRAC_W(FRAC_W + 4), .EPS_Q2FRAC(64'd687195)) no
        (.x_bus(head_raw[lane*DH*DATA_W +: DH*DATA_W]), .norm_offset_bus(onorm_offset_bus),
         .y_bus(head_norm[lane*DH*DATA_W +: DH*DATA_W]));
    end
  endgenerate

  // Linear interpolation helper shared by the alpha PWL fits below.
  function automatic logic signed [63:0] pwl_lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    pwl_lerp = y0 + ((x_value - x0) * (y1 - y0)) / (x1 - x0);
  endfunction

  // PWL interpolation for exp(-5*s), s in unsigned Q1.14.  The previous
  // twenty-two breakpoints left ~0.0046 error near s=0; after the dense
  // sigmoid fit above this is the remaining alpha-path approximation error.
  // exp(-5*s) is sampled every 0.02 (50 segments), exact at each sample,
  // monotonic and bounded.
  function automatic logic signed [DATA_W-1:0] decay_from_sigmoid(input logic [FRAC_W:0] s_in);
    logic signed [63:0] s;
    logic signed [63:0] y;
    begin
      s = $signed({1'b0, s_in});
      if (s <= 328) y = pwl_lerp(s, 0, 328, 16384, 14825);
      else if (s <= 655) y = pwl_lerp(s, 328, 655, 14825, 13414);
      else if (s <= 983) y = pwl_lerp(s, 655, 983, 13414, 12138);
      else if (s <= 1311) y = pwl_lerp(s, 983, 1311, 12138, 10983);
      else if (s <= 1638) y = pwl_lerp(s, 1311, 1638, 10983, 9937);
      else if (s <= 1966) y = pwl_lerp(s, 1638, 1966, 9937, 8992);
      else if (s <= 2294) y = pwl_lerp(s, 1966, 2294, 8992, 8136);
      else if (s <= 2621) y = pwl_lerp(s, 2294, 2621, 8136, 7362);
      else if (s <= 2949) y = pwl_lerp(s, 2621, 2949, 7362, 6661);
      else if (s <= 3277) y = pwl_lerp(s, 2949, 3277, 6661, 6027);
      else if (s <= 3604) y = pwl_lerp(s, 3277, 3604, 6027, 5454);
      else if (s <= 3932) y = pwl_lerp(s, 3604, 3932, 5454, 4935);
      else if (s <= 4260) y = pwl_lerp(s, 3932, 4260, 4935, 4465);
      else if (s <= 4588) y = pwl_lerp(s, 4260, 4588, 4465, 4040);
      else if (s <= 4915) y = pwl_lerp(s, 4588, 4915, 4040, 3656);
      else if (s <= 5243) y = pwl_lerp(s, 4915, 5243, 3656, 3308);
      else if (s <= 5571) y = pwl_lerp(s, 5243, 5571, 3308, 2993);
      else if (s <= 5898) y = pwl_lerp(s, 5571, 5898, 2993, 2708);
      else if (s <= 6226) y = pwl_lerp(s, 5898, 6226, 2708, 2451);
      else if (s <= 6554) y = pwl_lerp(s, 6226, 6554, 2451, 2217);
      else if (s <= 6881) y = pwl_lerp(s, 6554, 6881, 2217, 2006);
      else if (s <= 7209) y = pwl_lerp(s, 6881, 7209, 2006, 1815);
      else if (s <= 7537) y = pwl_lerp(s, 7209, 7537, 1815, 1643);
      else if (s <= 7864) y = pwl_lerp(s, 7537, 7864, 1643, 1486);
      else if (s <= 8192) y = pwl_lerp(s, 7864, 8192, 1486, 1345);
      else if (s <= 8520) y = pwl_lerp(s, 8192, 8520, 1345, 1217);
      else if (s <= 8847) y = pwl_lerp(s, 8520, 8847, 1217, 1101);
      else if (s <= 9175) y = pwl_lerp(s, 8847, 9175, 1101, 996);
      else if (s <= 9503) y = pwl_lerp(s, 9175, 9503, 996, 902);
      else if (s <= 9830) y = pwl_lerp(s, 9503, 9830, 902, 816);
      else if (s <= 10158) y = pwl_lerp(s, 9830, 10158, 816, 738);
      else if (s <= 10486) y = pwl_lerp(s, 10158, 10486, 738, 668);
      else if (s <= 10813) y = pwl_lerp(s, 10486, 10813, 668, 604);
      else if (s <= 11141) y = pwl_lerp(s, 10813, 11141, 604, 547);
      else if (s <= 11469) y = pwl_lerp(s, 11141, 11469, 547, 495);
      else if (s <= 11796) y = pwl_lerp(s, 11469, 11796, 495, 448);
      else if (s <= 12124) y = pwl_lerp(s, 11796, 12124, 448, 405);
      else if (s <= 12452) y = pwl_lerp(s, 12124, 12452, 405, 367);
      else if (s <= 12780) y = pwl_lerp(s, 12452, 12780, 367, 332);
      else if (s <= 13107) y = pwl_lerp(s, 12780, 13107, 332, 300);
      else if (s <= 13435) y = pwl_lerp(s, 13107, 13435, 300, 272);
      else if (s <= 13763) y = pwl_lerp(s, 13435, 13763, 272, 246);
      else if (s <= 14090) y = pwl_lerp(s, 13763, 14090, 246, 222);
      else if (s <= 14418) y = pwl_lerp(s, 14090, 14418, 222, 201);
      else if (s <= 14746) y = pwl_lerp(s, 14418, 14746, 201, 182);
      else if (s <= 15073) y = pwl_lerp(s, 14746, 15073, 182, 165);
      else if (s <= 15401) y = pwl_lerp(s, 15073, 15401, 165, 149);
      else if (s <= 15729) y = pwl_lerp(s, 15401, 15729, 149, 135);
      else if (s <= 16056) y = pwl_lerp(s, 15729, 16056, 135, 122);
      else y = pwl_lerp(s, 16056, 16384, 122, 110);
      if (y < 110) y = 110;
      decay_from_sigmoid = y;
    end
  endfunction

  // Dense sigmoid PWL for the alpha path.  alpha = exp(-5*sigmoid(...)) is
  // five times as sensitive to a sigmoid error as to a decay error, so the
  // previous 21-segment fit (0.5 spacing between -2 and -1 and between 1 and
  // 2) still produced ~0.02 alpha error at |x|~2.4.  This fit samples
  // sigmoid every 0.125 over [-4,4] and every 0.25 in the [-6,-4]/[4,6]
  // tails, reducing the alpha error to <0.002.  It matches sigmoid_pwl.
  function automatic logic [FRAC_W:0] sigmoid_from_q14(input logic signed [63:0] x);
    logic signed [63:0] y;
    begin
      if (x <= -131072) y = 5;
      else if (x <= -98304) y = pwl_lerp(x, -131072, -98304, 5, 41);
      else if (x <= -94208) y = pwl_lerp(x, -98304, -94208, 41, 52);
      else if (x <= -90112) y = pwl_lerp(x, -94208, -90112, 52, 67);
      else if (x <= -86016) y = pwl_lerp(x, -90112, -86016, 67, 86);
      else if (x <= -81920) y = pwl_lerp(x, -86016, -81920, 86, 110);
      else if (x <= -77824) y = pwl_lerp(x, -81920, -77824, 110, 141);
      else if (x <= -73728) y = pwl_lerp(x, -77824, -73728, 141, 180);
      else if (x <= -69632) y = pwl_lerp(x, -73728, -69632, 180, 230);
      else if (x <= -65536) y = pwl_lerp(x, -69632, -65536, 230, 295);
      else if (x <= -63488) y = pwl_lerp(x, -65536, -63488, 295, 333);
      else if (x <= -61440) y = pwl_lerp(x, -63488, -61440, 333, 376);
      else if (x <= -59392) y = pwl_lerp(x, -61440, -59392, 376, 425);
      else if (x <= -57344) y = pwl_lerp(x, -59392, -57344, 425, 480);
      else if (x <= -55296) y = pwl_lerp(x, -57344, -55296, 480, 542);
      else if (x <= -53248) y = pwl_lerp(x, -55296, -53248, 542, 612);
      else if (x <= -51200) y = pwl_lerp(x, -53248, -51200, 612, 690);
      else if (x <= -49152) y = pwl_lerp(x, -51200, -49152, 690, 777);
      else if (x <= -47104) y = pwl_lerp(x, -49152, -47104, 777, 875);
      else if (x <= -45056) y = pwl_lerp(x, -47104, -45056, 875, 984);
      else if (x <= -43008) y = pwl_lerp(x, -45056, -43008, 984, 1107);
      else if (x <= -40960) y = pwl_lerp(x, -43008, -40960, 1107, 1243);
      else if (x <= -38912) y = pwl_lerp(x, -40960, -38912, 1243, 1394);
      else if (x <= -36864) y = pwl_lerp(x, -38912, -36864, 1394, 1562);
      else if (x <= -34816) y = pwl_lerp(x, -36864, -34816, 1562, 1748);
      else if (x <= -32768) y = pwl_lerp(x, -34816, -32768, 1748, 1953);
      else if (x <= -30720) y = pwl_lerp(x, -32768, -30720, 1953, 2178);
      else if (x <= -28672) y = pwl_lerp(x, -30720, -28672, 2178, 2426);
      else if (x <= -26624) y = pwl_lerp(x, -28672, -26624, 2426, 2695);
      else if (x <= -24576) y = pwl_lerp(x, -26624, -24576, 2695, 2989);
      else if (x <= -22528) y = pwl_lerp(x, -24576, -22528, 2989, 3307);
      else if (x <= -20480) y = pwl_lerp(x, -22528, -20480, 3307, 3649);
      else if (x <= -18432) y = pwl_lerp(x, -20480, -18432, 3649, 4015);
      else if (x <= -16384) y = pwl_lerp(x, -18432, -16384, 4015, 4406);
      else if (x <= -14336) y = pwl_lerp(x, -16384, -14336, 4406, 4820);
      else if (x <= -12288) y = pwl_lerp(x, -14336, -12288, 4820, 5256);
      else if (x <= -10240) y = pwl_lerp(x, -12288, -10240, 5256, 5712);
      else if (x <= -8192) y = pwl_lerp(x, -10240, -8192, 5712, 6186);
      else if (x <= -6144) y = pwl_lerp(x, -8192, -6144, 6186, 6674);
      else if (x <= -4096) y = pwl_lerp(x, -6144, -4096, 6674, 7173);
      else if (x <= -2048) y = pwl_lerp(x, -4096, -2048, 7173, 7681);
      else if (x <= 0) y = pwl_lerp(x, -2048, 0, 7681, 8192);
      else if (x <= 2048) y = pwl_lerp(x, 0, 2048, 8192, 8703);
      else if (x <= 4096) y = pwl_lerp(x, 2048, 4096, 8703, 9211);
      else if (x <= 6144) y = pwl_lerp(x, 4096, 6144, 9211, 9710);
      else if (x <= 8192) y = pwl_lerp(x, 6144, 8192, 9710, 10198);
      else if (x <= 10240) y = pwl_lerp(x, 8192, 10240, 10198, 10672);
      else if (x <= 12288) y = pwl_lerp(x, 10240, 12288, 10672, 11128);
      else if (x <= 14336) y = pwl_lerp(x, 12288, 14336, 11128, 11564);
      else if (x <= 16384) y = pwl_lerp(x, 14336, 16384, 11564, 11978);
      else if (x <= 18432) y = pwl_lerp(x, 16384, 18432, 11978, 12369);
      else if (x <= 20480) y = pwl_lerp(x, 18432, 20480, 12369, 12735);
      else if (x <= 22528) y = pwl_lerp(x, 20480, 22528, 12735, 13077);
      else if (x <= 24576) y = pwl_lerp(x, 22528, 24576, 13077, 13395);
      else if (x <= 26624) y = pwl_lerp(x, 24576, 26624, 13395, 13689);
      else if (x <= 28672) y = pwl_lerp(x, 26624, 28672, 13689, 13958);
      else if (x <= 30720) y = pwl_lerp(x, 28672, 30720, 13958, 14206);
      else if (x <= 32768) y = pwl_lerp(x, 30720, 32768, 14206, 14431);
      else if (x <= 34816) y = pwl_lerp(x, 32768, 34816, 14431, 14636);
      else if (x <= 36864) y = pwl_lerp(x, 34816, 36864, 14636, 14822);
      else if (x <= 38912) y = pwl_lerp(x, 36864, 38912, 14822, 14990);
      else if (x <= 40960) y = pwl_lerp(x, 38912, 40960, 14990, 15141);
      else if (x <= 43008) y = pwl_lerp(x, 40960, 43008, 15141, 15277);
      else if (x <= 45056) y = pwl_lerp(x, 43008, 45056, 15277, 15400);
      else if (x <= 47104) y = pwl_lerp(x, 45056, 47104, 15400, 15509);
      else if (x <= 49152) y = pwl_lerp(x, 47104, 49152, 15509, 15607);
      else if (x <= 51200) y = pwl_lerp(x, 49152, 51200, 15607, 15694);
      else if (x <= 53248) y = pwl_lerp(x, 51200, 53248, 15694, 15772);
      else if (x <= 55296) y = pwl_lerp(x, 53248, 55296, 15772, 15842);
      else if (x <= 57344) y = pwl_lerp(x, 55296, 57344, 15842, 15904);
      else if (x <= 59392) y = pwl_lerp(x, 57344, 59392, 15904, 15959);
      else if (x <= 61440) y = pwl_lerp(x, 59392, 61440, 15959, 16008);
      else if (x <= 63488) y = pwl_lerp(x, 61440, 63488, 16008, 16051);
      else if (x <= 65536) y = pwl_lerp(x, 63488, 65536, 16051, 16089);
      else if (x <= 69632) y = pwl_lerp(x, 65536, 69632, 16089, 16154);
      else if (x <= 73728) y = pwl_lerp(x, 69632, 73728, 16154, 16204);
      else if (x <= 77824) y = pwl_lerp(x, 73728, 77824, 16204, 16243);
      else if (x <= 81920) y = pwl_lerp(x, 77824, 81920, 16243, 16274);
      else if (x <= 86016) y = pwl_lerp(x, 81920, 86016, 16274, 16298);
      else if (x <= 90112) y = pwl_lerp(x, 86016, 90112, 16298, 16317);
      else if (x <= 94208) y = pwl_lerp(x, 90112, 94208, 16317, 16332);
      else if (x <= 98304) y = pwl_lerp(x, 94208, 98304, 16332, 16343);
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
        // head_norm is Q*.18 (from the wider onorm); z_gate is Q1.14.  One
        // 18-bit rescale returns core to the external Q*.14 convention.
        output_product = $signed(head_norm[(h*DH+i)*DATA_W +: DATA_W]) *
                  $signed({1'b0, z_gate[(h*DH+i)*(FRAC_W+1) +: FRAC_W+1]});
        core_bus[(h*DH+i)*DATA_W +: DATA_W] = output_product >>> (FRAC_W + 4);
      end
    end
  end
endmodule
