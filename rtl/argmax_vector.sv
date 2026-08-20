// Stable argmax: strict > means the lower index wins ties, as required.
module argmax_vector #(
  parameter int ELEMS  = 512,
  parameter int DATA_W = 32,
  localparam int INDEX_W = $clog2(ELEMS)
) (
  input  logic signed [ELEMS*DATA_W-1:0] values_bus,
  output logic [INDEX_W-1:0]             index_out,
  output logic signed [DATA_W-1:0]       value_out
);
  integer i;
  logic signed [DATA_W-1:0] candidate;
  always_comb begin
    value_out = $signed(values_bus[0 +: DATA_W]);
    index_out = '0;
    for (i = 1; i < ELEMS; i = i + 1) begin
      candidate = $signed(values_bus[i*DATA_W +: DATA_W]);
      if (candidate > value_out) begin
        value_out = candidate;
        index_out = i[INDEX_W-1:0];
      end
    end
  end
endmodule
