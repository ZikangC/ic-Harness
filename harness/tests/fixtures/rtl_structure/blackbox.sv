(* blackbox *) module peripheral(input wire a, output wire y);
endmodule
module top(input wire a, output wire y);
  peripheral u_peripheral(.a(a), .y(y));
endmodule
