(* whitebox *) module arithmetic_macro(input wire a, output wire y);
  assign y = ~a;
endmodule
module top(input wire a, output wire y);
  arithmetic_macro u_macro(.a(a), .y(y));
endmodule
