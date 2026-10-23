module lanes #(parameter LANES = 64)(input logic [LANES*8-1:0] data,
                                    output logic [LANES*8-1:0] result);
  function automatic logic [7:0] twist(input logic [7:0] x);
    case (x[1:0])
      0: twist = (x + 8'd3) ^ 8'h55;
      1: twist = (x - 8'd7) ^ 8'haa;
      2: twist = {x[3:0], x[7:4]};
      default: twist = ~x;
    endcase
  endfunction
  always_comb begin
    for (int lane = 0; lane < LANES; lane++) begin
      result[lane*8 +: 8] = twist(data[lane*8 +: 8]);
    end
  end
endmodule
