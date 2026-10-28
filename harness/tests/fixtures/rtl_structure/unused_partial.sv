module top(input wire [3:0] a, output wire [3:0] y);
  wire [7:0] lane;
  assign lane[3:0] = a;
  assign y = lane[3:0];
endmodule
