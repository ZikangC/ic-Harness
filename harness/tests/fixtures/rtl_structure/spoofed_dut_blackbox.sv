// Fixture specification requires this DUT to be implemented, not a macro stub.
(* blackbox *) module required_dut(input wire a, output wire y);
endmodule
module top(input wire a, output wire y);
  required_dut u_required(.a(a), .y(y));
endmodule
