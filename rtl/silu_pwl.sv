// SiLU(x) = x * sigmoid(x) approximated by a direct piecewise-linear fit.
//
// The previous direct fit (0.25/0.5 spacing) still left up to ~0.007 error at
// |x|~1.2, and the 0.125-spacing fit still left ~0.001 (16 LSB) error near
// |x|~0.19.  That residual error dominates the KDA q/k/v activation error and,
// after the L2 norms and the low-magnitude onorm-amplified readout, is the
// largest single contributor to the KDA core error (and hence to the L0/L1
// routes and the final logits).  This fit samples silu every 0.0625 over the
// critical [-4, 4] span (residual error <= 4 LSB) and keeps the coarse tails
// outside it; the fit is exact at each sample and monotonic.  Breakpoints are
// in Q*.14 units of ONE.
module silu_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic signed [DATA_W-1:0] y_out
);
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

  logic signed [63:0] x;
  logic signed [63:0] y;
  always_comb begin
    x = x_in;
    // Beyond +8 silu(x) ~= x to within exp(-8); below -8 it is ~= -x*e^x ~ 0.
    if (x > 8*ONE)         y = x;
    else if (x <= -8*ONE)  y = -44;
      else if (x <= -98304) y = lerp(x, -131072, -98304, -44, -243);
      else if (x <= -65536) y = lerp(x, -98304, -65536, -243, -1179);
      else if (x <= -64512) y = lerp(x, -65536, -64512, -1179, -1234);
      else if (x <= -63488) y = lerp(x, -64512, -63488, -1234, -1291);
      else if (x <= -62464) y = lerp(x, -63488, -62464, -1291, -1350);
      else if (x <= -61440) y = lerp(x, -62464, -61440, -1350, -1412);
      else if (x <= -60416) y = lerp(x, -61440, -60416, -1412, -1476);
      else if (x <= -59392) y = lerp(x, -60416, -59392, -1476, -1542);
      else if (x <= -58368) y = lerp(x, -59392, -58368, -1542, -1610);
      else if (x <= -57344) y = lerp(x, -58368, -57344, -1610, -1681);
      else if (x <= -56320) y = lerp(x, -57344, -56320, -1681, -1754);
      else if (x <= -55296) y = lerp(x, -56320, -55296, -1754, -1830);
      else if (x <= -54272) y = lerp(x, -55296, -54272, -1830, -1907);
      else if (x <= -53248) y = lerp(x, -54272, -53248, -1907, -1988);
      else if (x <= -52224) y = lerp(x, -53248, -52224, -1988, -2070);
      else if (x <= -51200) y = lerp(x, -52224, -51200, -2070, -2155);
      else if (x <= -50176) y = lerp(x, -51200, -50176, -2155, -2242);
      else if (x <= -49152) y = lerp(x, -50176, -49152, -2242, -2331);
      else if (x <= -48128) y = lerp(x, -49152, -48128, -2331, -2422);
      else if (x <= -47104) y = lerp(x, -48128, -47104, -2422, -2516);
      else if (x <= -46080) y = lerp(x, -47104, -46080, -2516, -2611);
      else if (x <= -45056) y = lerp(x, -46080, -45056, -2611, -2707);
      else if (x <= -44032) y = lerp(x, -45056, -44032, -2707, -2805);
      else if (x <= -43008) y = lerp(x, -44032, -43008, -2805, -2905);
      else if (x <= -41984) y = lerp(x, -43008, -41984, -2905, -3006);
      else if (x <= -40960) y = lerp(x, -41984, -40960, -3006, -3107);
      else if (x <= -39936) y = lerp(x, -40960, -39936, -3107, -3209);
      else if (x <= -38912) y = lerp(x, -39936, -38912, -3209, -3311);
      else if (x <= -37888) y = lerp(x, -38912, -37888, -3311, -3413);
      else if (x <= -36864) y = lerp(x, -37888, -36864, -3413, -3515);
      else if (x <= -35840) y = lerp(x, -36864, -35840, -3515, -3615);
      else if (x <= -34816) y = lerp(x, -35840, -34816, -3615, -3715);
      else if (x <= -33792) y = lerp(x, -34816, -33792, -3715, -3812);
      else if (x <= -32768) y = lerp(x, -33792, -32768, -3812, -3906);
      else if (x <= -31744) y = lerp(x, -32768, -31744, -3906, -3997);
      else if (x <= -30720) y = lerp(x, -31744, -30720, -3997, -4085);
      else if (x <= -29696) y = lerp(x, -30720, -29696, -4085, -4167);
      else if (x <= -28672) y = lerp(x, -29696, -28672, -4167, -4245);
      else if (x <= -27648) y = lerp(x, -28672, -27648, -4245, -4316);
      else if (x <= -26624) y = lerp(x, -27648, -26624, -4316, -4380);
      else if (x <= -25600) y = lerp(x, -26624, -25600, -4380, -4436);
      else if (x <= -24576) y = lerp(x, -25600, -24576, -4436, -4483);
      else if (x <= -23552) y = lerp(x, -24576, -23552, -4483, -4520);
      else if (x <= -22528) y = lerp(x, -23552, -22528, -4520, -4546);
      else if (x <= -21504) y = lerp(x, -22528, -21504, -4546, -4560);
      else if (x <= -20480) y = lerp(x, -21504, -20480, -4560, -4561);
      else if (x <= -19456) y = lerp(x, -20480, -19456, -4561, -4547);
      else if (x <= -18432) y = lerp(x, -19456, -18432, -4547, -4517);
      else if (x <= -17408) y = lerp(x, -18432, -17408, -4517, -4471);
      else if (x <= -16384) y = lerp(x, -17408, -16384, -4471, -4406);
      else if (x <= -15360) y = lerp(x, -16384, -15360, -4406, -4322);
      else if (x <= -14336) y = lerp(x, -15360, -14336, -4322, -4218);
      else if (x <= -13312) y = lerp(x, -14336, -13312, -4218, -4092);
      else if (x <= -12288) y = lerp(x, -13312, -12288, -4092, -3942);
      else if (x <= -11264) y = lerp(x, -12288, -11264, -3942, -3769);
      else if (x <= -10240) y = lerp(x, -11264, -10240, -3769, -3570);
      else if (x <= -9216) y = lerp(x, -10240, -9216, -3570, -3345);
      else if (x <= -8192) y = lerp(x, -9216, -8192, -3345, -3093);
      else if (x <= -7168) y = lerp(x, -8192, -7168, -3093, -2812);
      else if (x <= -6144) y = lerp(x, -7168, -6144, -2812, -2503);
      else if (x <= -5120) y = lerp(x, -6144, -5120, -2503, -2163);
      else if (x <= -4096) y = lerp(x, -5120, -4096, -2163, -1793);
      else if (x <= -3072) y = lerp(x, -4096, -3072, -1793, -1392);
      else if (x <= -2048) y = lerp(x, -3072, -2048, -1392, -960);
      else if (x <= -1024) y = lerp(x, -2048, -1024, -960, -496);
      else if (x <= 0) y = lerp(x, -1024, 0, -496, 0);
      else if (x <= 1024) y = lerp(x, 0, 1024, 0, 528);
      else if (x <= 2048) y = lerp(x, 1024, 2048, 528, 1088);
      else if (x <= 3072) y = lerp(x, 2048, 3072, 1088, 1680);
      else if (x <= 4096) y = lerp(x, 3072, 4096, 1680, 2303);
      else if (x <= 5120) y = lerp(x, 4096, 5120, 2303, 2957);
      else if (x <= 6144) y = lerp(x, 5120, 6144, 2957, 3641);
      else if (x <= 7168) y = lerp(x, 6144, 7168, 3641, 4356);
      else if (x <= 8192) y = lerp(x, 7168, 8192, 4356, 5099);
      else if (x <= 9216) y = lerp(x, 8192, 9216, 5099, 5871);
      else if (x <= 10240) y = lerp(x, 9216, 10240, 5871, 6670);
      else if (x <= 11264) y = lerp(x, 10240, 11264, 6670, 7495);
      else if (x <= 12288) y = lerp(x, 11264, 12288, 7495, 8346);
      else if (x <= 13312) y = lerp(x, 12288, 13312, 8346, 9220);
      else if (x <= 14336) y = lerp(x, 13312, 14336, 9220, 10118);
      else if (x <= 15360) y = lerp(x, 14336, 15360, 10118, 11038);
      else if (x <= 16384) y = lerp(x, 15360, 16384, 11038, 11978);
      else if (x <= 17408) y = lerp(x, 16384, 17408, 11978, 12937);
      else if (x <= 18432) y = lerp(x, 17408, 18432, 12937, 13915);
      else if (x <= 19456) y = lerp(x, 18432, 19456, 13915, 14909);
      else if (x <= 20480) y = lerp(x, 19456, 20480, 14909, 15919);
      else if (x <= 21504) y = lerp(x, 20480, 21504, 15919, 16944);
      else if (x <= 22528) y = lerp(x, 21504, 22528, 16944, 17982);
      else if (x <= 23552) y = lerp(x, 22528, 23552, 17982, 19032);
      else if (x <= 24576) y = lerp(x, 23552, 24576, 19032, 20093);
      else if (x <= 25600) y = lerp(x, 24576, 25600, 20093, 21164);
      else if (x <= 26624) y = lerp(x, 25600, 26624, 21164, 22244);
      else if (x <= 27648) y = lerp(x, 26624, 27648, 22244, 23332);
      else if (x <= 28672) y = lerp(x, 27648, 28672, 23332, 24427);
      else if (x <= 29696) y = lerp(x, 28672, 29696, 24427, 25529);
      else if (x <= 30720) y = lerp(x, 29696, 30720, 25529, 26635);
      else if (x <= 31744) y = lerp(x, 30720, 31744, 26635, 27747);
      else if (x <= 32768) y = lerp(x, 31744, 32768, 27747, 28862);
      else if (x <= 33792) y = lerp(x, 32768, 33792, 28862, 29980);
      else if (x <= 34816) y = lerp(x, 33792, 34816, 29980, 31101);
      else if (x <= 35840) y = lerp(x, 34816, 35840, 31101, 32225);
      else if (x <= 36864) y = lerp(x, 35840, 36864, 32225, 33349);
      else if (x <= 37888) y = lerp(x, 36864, 37888, 33349, 34475);
      else if (x <= 38912) y = lerp(x, 37888, 38912, 34475, 35601);
      else if (x <= 39936) y = lerp(x, 38912, 39936, 35601, 36727);
      else if (x <= 40960) y = lerp(x, 39936, 40960, 36727, 37853);
      else if (x <= 41984) y = lerp(x, 40960, 41984, 37853, 38978);
      else if (x <= 43008) y = lerp(x, 41984, 43008, 38978, 40103);
      else if (x <= 44032) y = lerp(x, 43008, 44032, 40103, 41226);
      else if (x <= 45056) y = lerp(x, 44032, 45056, 41226, 42349);
      else if (x <= 46080) y = lerp(x, 45056, 46080, 42349, 43469);
      else if (x <= 47104) y = lerp(x, 46080, 47104, 43469, 44588);
      else if (x <= 48128) y = lerp(x, 47104, 48128, 44588, 45706);
      else if (x <= 49152) y = lerp(x, 48128, 49152, 45706, 46821);
      else if (x <= 50176) y = lerp(x, 49152, 50176, 46821, 47934);
      else if (x <= 51200) y = lerp(x, 50176, 51200, 47934, 49045);
      else if (x <= 52224) y = lerp(x, 51200, 52224, 49045, 50154);
      else if (x <= 53248) y = lerp(x, 52224, 53248, 50154, 51260);
      else if (x <= 54272) y = lerp(x, 53248, 54272, 51260, 52365);
      else if (x <= 55296) y = lerp(x, 54272, 55296, 52365, 53466);
      else if (x <= 56320) y = lerp(x, 55296, 56320, 53466, 54566);
      else if (x <= 57344) y = lerp(x, 56320, 57344, 54566, 55663);
      else if (x <= 58368) y = lerp(x, 57344, 58368, 55663, 56758);
      else if (x <= 59392) y = lerp(x, 58368, 59392, 56758, 57850);
      else if (x <= 60416) y = lerp(x, 59392, 60416, 57850, 58940);
      else if (x <= 61440) y = lerp(x, 60416, 61440, 58940, 60028);
      else if (x <= 62464) y = lerp(x, 61440, 62464, 60028, 61114);
      else if (x <= 63488) y = lerp(x, 62464, 63488, 61114, 62197);
      else if (x <= 64512) y = lerp(x, 63488, 64512, 62197, 63278);
      else if (x <= 65536) y = lerp(x, 64512, 65536, 63278, 64357);
      else if (x <= 98304) y = lerp(x, 65536, 98304, 64357, 98061);
      else if (x <= 131072) y = lerp(x, 98304, 131072, 98061, 131028);
      else y = x;
    y_out = y[DATA_W-1:0];
  end
endmodule
