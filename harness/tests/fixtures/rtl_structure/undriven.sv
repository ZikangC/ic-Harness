module top(input wire a, output wire y);
  wire missing;
  assign y = a ^ missing;
endmodule
