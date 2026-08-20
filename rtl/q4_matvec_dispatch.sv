// Selects the fixed shapes used by nano from one q/s/z matrix buffer.  This
// is intentionally combinational: the adapter owns scheduling and captures
// y_bus only after matrix_qsz_buffer reports all_loaded.
module q4_matvec_dispatch #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14,
  parameter int MAX_Q_BEATS = 1024,
  parameter int MAX_S_BEATS = 64,
  parameter int MAX_Z_BEATS = 64
) (
  input  logic [3:0] kind,
  input  logic signed [64*DATA_W-1:0] x_bus,
  input  logic [MAX_Q_BEATS*128-1:0] q_bus,
  input  logic [MAX_S_BEATS*128-1:0] s_bus,
  input  logic [MAX_Z_BEATS*128-1:0] z_bus,
  output logic signed [512*DATA_W-1:0] y_bus
);
  localparam logic [3:0]
    K_64X64  = 4'd0,
    K_64X32  = 4'd1,
    K_32X64  = 4'd2,
    K_64X96  = 4'd3,
    K_64X8   = 4'd4,
    K_64X1   = 4'd5,
    K_64X512 = 4'd6;

  logic signed [64*DATA_W-1:0] y64, y32to64;
  logic signed [32*DATA_W-1:0] y32;
  logic signed [96*DATA_W-1:0] y96;
  logic signed [8*DATA_W-1:0] y8;
  logic signed [DATA_W-1:0] y1;
  logic signed [512*DATA_W-1:0] y512;

  q4_dequant_matvec #(.IN(64), .OUT(64), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x64 (
    .x_bus(x_bus), .q_bus(kind==K_64X64 ? q_bus[64/2*64*8-1:0] : '0), .scale_bus(kind==K_64X64 ? s_bus[64*16-1:0] : '0),
    .zero_bus(kind==K_64X64 ? z_bus[64*8-1:0] : '0), .y_bus(y64)
  );
  q4_dequant_matvec #(.IN(64), .OUT(32), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x32 (
    .x_bus(x_bus), .q_bus(kind==K_64X32 ? q_bus[64/2*32*8-1:0] : '0), .scale_bus(kind==K_64X32 ? s_bus[32*16-1:0] : '0),
    .zero_bus(kind==K_64X32 ? z_bus[32*8-1:0] : '0), .y_bus(y32)
  );
  q4_dequant_matvec #(.IN(32), .OUT(64), .GROUP(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm32x64 (
    .x_bus(x_bus[32*DATA_W-1:0]), .q_bus(kind==K_32X64 ? q_bus[32/2*64*8-1:0] : '0), .scale_bus(kind==K_32X64 ? s_bus[64*16-1:0] : '0),
    .zero_bus(kind==K_32X64 ? z_bus[64*8-1:0] : '0), .y_bus(y32to64)
  );
  q4_dequant_matvec #(.IN(64), .OUT(96), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x96 (
    .x_bus(x_bus), .q_bus(kind==K_64X96 ? q_bus[64/2*96*8-1:0] : '0), .scale_bus(kind==K_64X96 ? s_bus[96*16-1:0] : '0),
    .zero_bus(kind==K_64X96 ? z_bus[96*8-1:0] : '0), .y_bus(y96)
  );
  q4_dequant_matvec #(.IN(64), .OUT(8), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x8 (
    .x_bus(x_bus), .q_bus(kind==K_64X8 ? q_bus[64/2*8*8-1:0] : '0), .scale_bus(kind==K_64X8 ? s_bus[8*16-1:0] : '0),
    .zero_bus(kind==K_64X8 ? z_bus[8*8-1:0] : '0), .y_bus(y8)
  );
  q4_dequant_matvec #(.IN(64), .OUT(1), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x1 (
    .x_bus(x_bus), .q_bus(kind==K_64X1 ? q_bus[64/2*1*8-1:0] : '0), .scale_bus(kind==K_64X1 ? s_bus[16-1:0] : '0),
    .zero_bus(kind==K_64X1 ? z_bus[8-1:0] : '0), .y_bus(y1)
  );
  q4_dequant_matvec #(.IN(64), .OUT(512), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) mm64x512 (
    .x_bus(x_bus), .q_bus(kind==K_64X512 ? q_bus[64/2*512*8-1:0] : '0), .scale_bus(kind==K_64X512 ? s_bus[512*16-1:0] : '0),
    .zero_bus(kind==K_64X512 ? z_bus[512*8-1:0] : '0), .y_bus(y512)
  );

  always_comb begin
    y_bus = '0;
    case (kind)
      K_64X64:  y_bus[64*DATA_W-1:0] = y64;
      K_64X32:  y_bus[32*DATA_W-1:0] = y32;
      K_32X64:  y_bus[64*DATA_W-1:0] = y32to64;
      K_64X96:  y_bus[96*DATA_W-1:0] = y96;
      K_64X8:   y_bus[8*DATA_W-1:0] = y8;
      K_64X1:   y_bus[DATA_W-1:0] = y1;
      K_64X512: y_bus = y512;
      default: ;
    endcase
  end
endmodule
