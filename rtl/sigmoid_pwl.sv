// Shared sigmoid approximation. Input is signed Q*.14; output is unsigned
// Q1.14. Breakpoints are symmetric, monotonic PWL samples of sigmoid(x).
//
// The previous dense fit still left ~0.007 error at |x|~2.4 (0.5/1.0 spacing
// between -2 and -1.5, and between 2 and 3).  That error is amplified by the
// KDA alpha path (alpha = exp(-5*sigmoid(...)), d alpha/d sigmoid = -5*alpha)
// and directly by the KDA z gate (core = head_norm * sigmoid(z)).  This fit
// samples sigmoid every 0.125 over the critical [-4, 4] span and every 0.25
// in the [-6,-4]/[4,6] tails, removing that error source while keeping the
// same Q1.14 output contract for the residual-mix weights, KDA z/beta gates
// and the legacy smoke adapter.
module sigmoid_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic        [FRAC_W:0]   y_out
);
  logic signed [63:0] x;
  logic signed [63:0] y;
  localparam logic signed [63:0] ONE = 64'sd1 <<< FRAC_W;

  function automatic logic signed [63:0] lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    lerp = y0 + ((x_value - x0) * (y1 - y0)) / (x1 - x0);
  endfunction

  always_comb begin
    x = x_in;
    if (x <= -131072) y = 5;
    else if (x <= -98304) y = lerp(x, -131072, -98304, 5, 41);
    else if (x <= -94208) y = lerp(x, -98304, -94208, 41, 52);
    else if (x <= -90112) y = lerp(x, -94208, -90112, 52, 67);
    else if (x <= -86016) y = lerp(x, -90112, -86016, 67, 86);
    else if (x <= -81920) y = lerp(x, -86016, -81920, 86, 110);
    else if (x <= -77824) y = lerp(x, -81920, -77824, 110, 141);
    else if (x <= -73728) y = lerp(x, -77824, -73728, 141, 180);
    else if (x <= -69632) y = lerp(x, -73728, -69632, 180, 230);
    else if (x <= -65536) y = lerp(x, -69632, -65536, 230, 295);
    else if (x <= -63488) y = lerp(x, -65536, -63488, 295, 333);
    else if (x <= -61440) y = lerp(x, -63488, -61440, 333, 376);
    else if (x <= -59392) y = lerp(x, -61440, -59392, 376, 425);
    else if (x <= -57344) y = lerp(x, -59392, -57344, 425, 480);
    else if (x <= -55296) y = lerp(x, -57344, -55296, 480, 542);
    else if (x <= -53248) y = lerp(x, -55296, -53248, 542, 612);
    else if (x <= -51200) y = lerp(x, -53248, -51200, 612, 690);
    else if (x <= -49152) y = lerp(x, -51200, -49152, 690, 777);
    else if (x <= -47104) y = lerp(x, -49152, -47104, 777, 875);
    else if (x <= -45056) y = lerp(x, -47104, -45056, 875, 984);
    else if (x <= -43008) y = lerp(x, -45056, -43008, 984, 1107);
    else if (x <= -40960) y = lerp(x, -43008, -40960, 1107, 1243);
    else if (x <= -38912) y = lerp(x, -40960, -38912, 1243, 1394);
    else if (x <= -36864) y = lerp(x, -38912, -36864, 1394, 1562);
    else if (x <= -34816) y = lerp(x, -36864, -34816, 1562, 1748);
    else if (x <= -32768) y = lerp(x, -34816, -32768, 1748, 1953);
    else if (x <= -30720) y = lerp(x, -32768, -30720, 1953, 2178);
    else if (x <= -28672) y = lerp(x, -30720, -28672, 2178, 2426);
    else if (x <= -26624) y = lerp(x, -28672, -26624, 2426, 2695);
    else if (x <= -24576) y = lerp(x, -26624, -24576, 2695, 2989);
    else if (x <= -22528) y = lerp(x, -24576, -22528, 2989, 3307);
    else if (x <= -20480) y = lerp(x, -22528, -20480, 3307, 3649);
    else if (x <= -18432) y = lerp(x, -20480, -18432, 3649, 4015);
    else if (x <= -16384) y = lerp(x, -18432, -16384, 4015, 4406);
    else if (x <= -14336) y = lerp(x, -16384, -14336, 4406, 4820);
    else if (x <= -12288) y = lerp(x, -14336, -12288, 4820, 5256);
    else if (x <= -10240) y = lerp(x, -12288, -10240, 5256, 5712);
    else if (x <= -8192) y = lerp(x, -10240, -8192, 5712, 6186);
    else if (x <= -6144) y = lerp(x, -8192, -6144, 6186, 6674);
    else if (x <= -4096) y = lerp(x, -6144, -4096, 6674, 7173);
    else if (x <= -2048) y = lerp(x, -4096, -2048, 7173, 7681);
    else if (x <= 0) y = lerp(x, -2048, 0, 7681, 8192);
    else if (x <= 2048) y = lerp(x, 0, 2048, 8192, 8703);
    else if (x <= 4096) y = lerp(x, 2048, 4096, 8703, 9211);
    else if (x <= 6144) y = lerp(x, 4096, 6144, 9211, 9710);
    else if (x <= 8192) y = lerp(x, 6144, 8192, 9710, 10198);
    else if (x <= 10240) y = lerp(x, 8192, 10240, 10198, 10672);
    else if (x <= 12288) y = lerp(x, 10240, 12288, 10672, 11128);
    else if (x <= 14336) y = lerp(x, 12288, 14336, 11128, 11564);
    else if (x <= 16384) y = lerp(x, 14336, 16384, 11564, 11978);
    else if (x <= 18432) y = lerp(x, 16384, 18432, 11978, 12369);
    else if (x <= 20480) y = lerp(x, 18432, 20480, 12369, 12735);
    else if (x <= 22528) y = lerp(x, 20480, 22528, 12735, 13077);
    else if (x <= 24576) y = lerp(x, 22528, 24576, 13077, 13395);
    else if (x <= 26624) y = lerp(x, 24576, 26624, 13395, 13689);
    else if (x <= 28672) y = lerp(x, 26624, 28672, 13689, 13958);
    else if (x <= 30720) y = lerp(x, 28672, 30720, 13958, 14206);
    else if (x <= 32768) y = lerp(x, 30720, 32768, 14206, 14431);
    else if (x <= 34816) y = lerp(x, 32768, 34816, 14431, 14636);
    else if (x <= 36864) y = lerp(x, 34816, 36864, 14636, 14822);
    else if (x <= 38912) y = lerp(x, 36864, 38912, 14822, 14990);
    else if (x <= 40960) y = lerp(x, 38912, 40960, 14990, 15141);
    else if (x <= 43008) y = lerp(x, 40960, 43008, 15141, 15277);
    else if (x <= 45056) y = lerp(x, 43008, 45056, 15277, 15400);
    else if (x <= 47104) y = lerp(x, 45056, 47104, 15400, 15509);
    else if (x <= 49152) y = lerp(x, 47104, 49152, 15509, 15607);
    else if (x <= 51200) y = lerp(x, 49152, 51200, 15607, 15694);
    else if (x <= 53248) y = lerp(x, 51200, 53248, 15694, 15772);
    else if (x <= 55296) y = lerp(x, 53248, 55296, 15772, 15842);
    else if (x <= 57344) y = lerp(x, 55296, 57344, 15842, 15904);
    else if (x <= 59392) y = lerp(x, 57344, 59392, 15904, 15959);
    else if (x <= 61440) y = lerp(x, 59392, 61440, 15959, 16008);
    else if (x <= 63488) y = lerp(x, 61440, 63488, 16008, 16051);
    else if (x <= 65536) y = lerp(x, 63488, 65536, 16051, 16089);
    else if (x <= 69632) y = lerp(x, 65536, 69632, 16089, 16154);
    else if (x <= 73728) y = lerp(x, 69632, 73728, 16154, 16204);
    else if (x <= 77824) y = lerp(x, 73728, 77824, 16204, 16243);
    else if (x <= 81920) y = lerp(x, 77824, 81920, 16243, 16274);
    else if (x <= 86016) y = lerp(x, 81920, 86016, 16274, 16298);
    else if (x <= 90112) y = lerp(x, 86016, 90112, 16298, 16317);
    else if (x <= 94208) y = lerp(x, 90112, 94208, 16317, 16332);
    else if (x <= 98304) y = lerp(x, 94208, 98304, 16332, 16343);
    else                y = 16384;
    y_out = y[FRAC_W:0];
  end
endmodule
