// Generated alongside golden_name_hash_pkg.sv; do not hand-edit.
package golden_tensor_meta_pkg;
  typedef struct packed {logic [31:0] q_hash,s_hash,z_hash; logic [31:0] q_bytes,s_bytes,z_bytes;} qsz_meta_t;
  function automatic qsz_meta_t matrix_meta(input logic [7:0] id, input logic group_is_128);
    qsz_meta_t m; begin m='0; case (id)
      8'd0: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_Q_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_Q_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_Q_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd1: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_K_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_K_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_K_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd2: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_V_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_V_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_V_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd3: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_B_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_B_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_B_Z;
        m.q_bytes=32'd64; m.s_bytes=32'd4; m.z_bytes=32'd2; end
      8'd4: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_F1_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_F1_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_F1_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd5: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_F2_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_F2_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_F2_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd6: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_G1_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_G1_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_G1_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd7: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_G2_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_G2_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_G2_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd8: begin m.q_hash=golden_name_hash_pkg::H_L0_KDA_O_Q; m.s_hash=golden_name_hash_pkg::H_L0_KDA_O_S; m.z_hash=golden_name_hash_pkg::H_L0_KDA_O_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd9: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_ROUTER_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_ROUTER_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_ROUTER_Z;
        m.q_bytes=32'd256; m.s_bytes=32'd16; m.z_bytes=32'd8; end
      8'd10: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd11: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd12: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_SHARED_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd13: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E0_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E0_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E0_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd14: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E0_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E0_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E0_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd15: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E0_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E0_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E0_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd16: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E1_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E1_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E1_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd17: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E1_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E1_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E1_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd18: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E1_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E1_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E1_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd19: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E2_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E2_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E2_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd20: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E2_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E2_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E2_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd21: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E2_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E2_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E2_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd22: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E3_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E3_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E3_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd23: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E3_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E3_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E3_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd24: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E3_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E3_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E3_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd25: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E4_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E4_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E4_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd26: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E4_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E4_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E4_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd27: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E4_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E4_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E4_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd28: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E5_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E5_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E5_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd29: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E5_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E5_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E5_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd30: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E5_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E5_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E5_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd31: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E6_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E6_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E6_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd32: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E6_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E6_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E6_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd33: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E6_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E6_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E6_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd34: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E7_G_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E7_G_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E7_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd35: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E7_U_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E7_U_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E7_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd36: begin m.q_hash=golden_name_hash_pkg::H_L0_MOE_E7_D_Q; m.s_hash=golden_name_hash_pkg::H_L0_MOE_E7_D_S; m.z_hash=golden_name_hash_pkg::H_L0_MOE_E7_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd37: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WC_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WC_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WC_Z;
        m.q_bytes=32'd4096; m.s_bytes=32'd256; m.z_bytes=32'd128; end
      8'd38: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WQ_C_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WQ_C_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WQ_C_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd39: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WQ_R_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WQ_R_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WQ_R_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd40: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WK_C_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WK_C_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WK_C_Z;
        m.q_bytes=32'd4096; m.s_bytes=32'd128; m.z_bytes=32'd64; if (!group_is_128) begin m.s_bytes=32'd256; m.z_bytes=32'd128; end end
      8'd41: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WK_R_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WK_R_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WK_R_Z;
        m.q_bytes=32'd512; m.s_bytes=32'd32; m.z_bytes=32'd16; end
      8'd42: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_WV_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_WV_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_WV_Z;
        m.q_bytes=32'd4096; m.s_bytes=32'd128; m.z_bytes=32'd64; if (!group_is_128) begin m.s_bytes=32'd256; m.z_bytes=32'd128; end end
      8'd43: begin m.q_hash=golden_name_hash_pkg::H_L1_MLA_O_Q; m.s_hash=golden_name_hash_pkg::H_L1_MLA_O_S; m.z_hash=golden_name_hash_pkg::H_L1_MLA_O_Z;
        m.q_bytes=32'd2048; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd44: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_ROUTER_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_ROUTER_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_ROUTER_Z;
        m.q_bytes=32'd256; m.s_bytes=32'd16; m.z_bytes=32'd8; end
      8'd45: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd46: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd47: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_SHARED_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd48: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E0_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E0_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E0_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd49: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E0_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E0_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E0_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd50: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E0_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E0_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E0_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd51: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E1_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E1_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E1_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd52: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E1_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E1_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E1_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd53: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E1_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E1_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E1_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd54: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E2_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E2_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E2_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd55: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E2_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E2_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E2_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd56: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E2_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E2_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E2_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd57: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E3_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E3_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E3_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd58: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E3_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E3_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E3_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd59: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E3_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E3_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E3_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd60: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E4_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E4_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E4_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd61: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E4_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E4_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E4_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd62: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E4_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E4_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E4_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd63: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E5_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E5_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E5_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd64: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E5_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E5_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E5_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd65: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E5_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E5_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E5_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd66: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E6_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E6_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E6_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd67: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E6_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E6_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E6_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd68: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E6_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E6_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E6_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd69: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E7_G_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E7_G_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E7_G_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd70: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E7_U_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E7_U_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E7_U_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd64; m.z_bytes=32'd32; end
      8'd71: begin m.q_hash=golden_name_hash_pkg::H_L1_MOE_E7_D_Q; m.s_hash=golden_name_hash_pkg::H_L1_MOE_E7_D_S; m.z_hash=golden_name_hash_pkg::H_L1_MOE_E7_D_Z;
        m.q_bytes=32'd1024; m.s_bytes=32'd128; m.z_bytes=32'd64; end
      8'd72: begin m.q_hash=golden_name_hash_pkg::H_HEAD_Q; m.s_hash=golden_name_hash_pkg::H_HEAD_S; m.z_hash=golden_name_hash_pkg::H_HEAD_Z;
        m.q_bytes=32'd16384; m.s_bytes=32'd1024; m.z_bytes=32'd512; end
      default: ; endcase matrix_meta=m; end endfunction
endpackage
