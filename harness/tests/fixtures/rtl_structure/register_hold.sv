module top(input wire clk, input wire en, input wire d, output reg q);
  always @(posedge clk)
    if (en) q <= d;
endmodule
