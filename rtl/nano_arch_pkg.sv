package nano_arch_pkg;
  // Internal fixed-point convention for this independent RTL baseline.
  // Values are signed Q9.14 unless a module documents another convention.
  parameter int DATA_W = 24;
  parameter int FRAC_W = 14;

  function automatic logic signed [DATA_W-1:0] sat_data(
      input logic signed [63:0] value
  );
    logic signed [63:0] max_value;
    logic signed [63:0] min_value;
    begin
      max_value = (64'sd1 <<< (DATA_W - 1)) - 1;
      min_value = -(64'sd1 <<< (DATA_W - 1));
      if (value > max_value)
        sat_data = max_value[DATA_W-1:0];
      else if (value < min_value)
        sat_data = min_value[DATA_W-1:0];
      else
        sat_data = value[DATA_W-1:0];
    end
  endfunction

  function automatic logic signed [DATA_W-1:0] mul_q(
      input logic signed [DATA_W-1:0] a,
      input logic signed [DATA_W-1:0] b
  );
    logic signed [2*DATA_W-1:0] product;
    logic signed [63:0] wide_product;
    begin
      product = a * b;
      wide_product = product;
      mul_q = sat_data(wide_product >>> FRAC_W);
    end
  endfunction
endpackage
