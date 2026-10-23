module tiny(input logic clk, input logic [7:0] data, output logic [7:0] q);
  always_ff @(posedge clk) q <= data;
endmodule
