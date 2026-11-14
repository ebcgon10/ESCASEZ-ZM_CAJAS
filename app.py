import io
import os
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import motor

st.set_page_config(page_title="Escasez ZM_CAJAS | CD Coquimbo", page_icon="📦", layout="wide")
BASE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(BASE, "maestros")

VERDE = "#5BB030"
VERDE_OSC = "#205C40"
st.markdown(f"""<style>
.stApp {{ background-color: #DCEBD9; }}
h1, h2, h3, .titulo {{ color: {VERDE_OSC} !important; }}
.titulo {{ font-size: 2.6rem; font-weight: 800; margin: 0.5rem 0 1rem 0; }}
.subt {{ font-size: 1.6rem; font-weight: 700; color: {VERDE_OSC}; margin-bottom: .3rem; }}
div[data-testid="stFileUploader"] {{ background: #fff; border-radius: 14px; padding: 12px 18px; }}
div.stButton > button {{ background-color: {VERDE}; color: #fff; font-weight: 700; font-size: 1.1rem;
    border: none; border-radius: 8px; padding: .75rem 0; width: 100%; }}
div.stButton > button:hover {{ background-color: #4E9A28; color: #fff; }}
header[data-testid="stHeader"] {{ background: #DCEBD9; }}
header[data-testid="stHeader"] * {{ color: {VERDE_OSC} !important; }}
div[data-testid="stFileUploader"] label p {{ color: {VERDE_OSC} !important; font-weight: 600; }}
section[data-testid="stFileUploaderDropzone"] {{ background: #F2F8F0 !important; border: 2px dashed {VERDE} !important;
    color: {VERDE_OSC} !important; }}
section[data-testid="stFileUploaderDropzone"] * {{ color: {VERDE_OSC} !important; }}
div[data-testid="stFileUploaderFile"], div[data-testid="stFileUploaderFile"] * {{ color: {VERDE_OSC} !important; }}
div[data-testid="stFileUploader"] button {{ background: #fff !important; color: {VERDE_OSC} !important;
    border: 1px solid {VERDE} !important; }}
div[data-testid="stAlert"] {{ background: #FFFFFF !important; border-left: 6px solid {VERDE} !important;
    border-radius: 10px; }}
div[data-testid="stAlert"] * {{ color: {VERDE_OSC} !important; font-weight: 600; }}
div[data-testid="stDownloadButton"] button {{ background: #fff !important; color: {VERDE_OSC} !important;
    border: 2px solid {VERDE} !important; font-weight: 700; }}
hr {{ border-color: #B9D3B2 !important; }}
</style>""", unsafe_allow_html=True)

st.markdown('<div class="titulo">Escasez ZM_CAJAS · Reposición antes de liberar</div>', unsafe_allow_html=True)


@st.cache_data
def maestros():
    ub, pr, zm = motor.cargar_maestros(os.path.join(M, "ubicaciones.csv"), os.path.join(M, "preferencia.csv"),
                                       os.path.join(M, "zonas.csv"))
    return ub, pr, zm, motor.cargar_paletizado(os.path.join(M, "paletizado.csv"))


@st.cache_data(ttl=3600)
def bbdd():
    return motor.cargar_bbdd(os.path.join(M, "bbdd_codigos.csv"))


c1, c2 = st.columns(2)
with c1:
    st.markdown('<div class="subt">1. Fleetloader (venta por camión)</div>', unsafe_allow_html=True)
    f_fleet = st.file_uploader("Archivo fleetloader", type=["txt", "csv", "xlsx"], key="fleet")
with c2:
    st.markdown('<div class="subt">2. Operaciones de Vida Útil</div>', unsafe_allow_html=True)
    f_vu = st.file_uploader("Archivo de inventario", type=["csv"], key="vu")

st.divider()
if st.button("🚀 Procesar Reposición ZM_CAJAS", use_container_width=True):
    if not (f_fleet and f_vu):
        st.error("Sube los 2 archivos: fleetloader y operaciones de vida útil.")
    else:
        try:
            ub, pr, zm, pal = maestros()
            bb, _ = bbdd()
            raw, cols = motor.leer_fleetloader(f_fleet)
            if not all(cols.get(k) for k in ["camion", "codigo", "cantidad"]):
                raise ValueError("el fleetloader no trae las columnas ROUTE_ID / SKU / QUANTITY")
            fleet = motor.preparar_fleet(raw, cols, bb, pal)
            vu = motor.cargar_vida_util(f_vu)
            hoy = datetime.now(ZoneInfo("America/Santiago")).date()
            resumen, movs, *_ = motor.calcular(fleet, vu, ub, pr, zm, pal, fecha_ref=hoy, bajar_restos=False)
            st.session_state["hoja"] = motor.hoja_reposicion(resumen, movs)
            st.session_state["emision"] = datetime.now(ZoneInfo("America/Santiago")).strftime("%d/%m/%Y %H:%M")
        except Exception as e:
            st.session_state.pop("hoja", None)
            st.error(f"No se pudo procesar: {e}")

if "hoja" in st.session_state:
    hoja = st.session_state["hoja"]
    emision = st.session_state["emision"]
    if hoja.empty:
        st.success("No hay nada que reponer: el stock en las posiciones ZM_CAJA cubre toda la venta. 🎉")
        st.stop()

    n_sku, n_filas, n_cajas = hoja["SKU"].nunique(), len(hoja), int(hoja["Cantidad"].sum())
    st.success(f"Procesamiento exitoso: {n_sku} SKUs, {n_filas} filas.")

    filas = ""
    pasillo_ant = None
    for i, (pas, sku, desc, org, dst, cant) in enumerate(hoja.itertuples(index=False, name=None), start=1):
        if pas != pasillo_ant:
            filas += f'<tr class="pas"><td colspan="7">PASILLO {escape(str(pas))}</td></tr>'
            pasillo_ant = pas
        sin = org == "SIN STOCK"
        filas += f"""<tr{' class="sin"' if sin else ''}><td class="c">{i}</td><td class="sku">{escape(str(sku))}</td>
          <td>{escape(str(desc))}</td><td class="c org">{escape(str(org))}</td>
          <td class="c dst">{escape(str(dst))}</td><td class="c qty">{cant}</td><td class="chk"></td></tr>"""

    html = f"""<style>
      body {{ font-family: Arial, sans-serif; margin: 0; }}
      .card {{ border: 3px solid #1E293B; border-radius: 10px; padding: 20px 26px; background: #fff; color: #0F172A; }}
      .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
      .t {{ font-size: 30px; font-weight: bold; text-transform: uppercase; }}
      .em {{ font-size: 15px; }} .tot {{ font-size: 21px; margin: 6px 0 10px 0; }}
      table {{ width: 100%; border-collapse: collapse; }}
      th {{ background: #1E293B; color: #fff; padding: 9px 6px; font-size: 14px; text-transform: uppercase; }}
      td {{ border: 1px solid #94A3B8; padding: 9px 6px; font-size: 16px; }}
      .c {{ text-align: center; }} .sku {{ font-family: monospace; font-weight: bold; font-size: 15px; }}
      .org {{ color: #1E3A8A; font-weight: bold; }} .dst {{ color: #065F46; font-weight: bold; }}
      .qty {{ font-size: 20px; font-weight: bold; }} .chk {{ width: 90px; }}
      tr.pas td {{ background: #E2E8F0; font-weight: bold; font-size: 14px; letter-spacing: 1px; }}
      tr.sin td {{ background: #FDE2E2; }} tr.sin .org {{ color: #A6093D; }}
      .btn {{ background: {VERDE}; color: #fff; font-weight: bold; padding: 12px 26px; font-size: 16px;
              border: none; border-radius: 8px; cursor: pointer; margin-top: 14px; }}
      @media print {{ .no-print {{ display: none !important; }} .card {{ border: none; padding: 0; }}
        tr {{ page-break-inside: avoid; }} thead {{ display: table-header-group; }}
        @page {{ size: A4 landscape; margin: 10mm; }} }}
    </style>
    <div class="card">
      <div class="top"><div class="t">Reposición ZM_CAJAS / Hoja de ruta</div>
        <div class="em"><b>Fecha emisión:</b> {emision} hrs</div></div>
      <div class="tot"><b>Total SKUs:</b> {n_sku} &nbsp;|&nbsp; <b>Total filas:</b> {n_filas}
        &nbsp;|&nbsp; <b>Total cajas:</b> {n_cajas}</div>
      <table><thead><tr><th>#</th><th>SKU</th><th>Descripción producto</th><th>Origen (almacenamiento)</th>
        <th>Destino (surtido)</th><th>Cantidad</th><th>Check ✓</th></tr></thead><tbody>{filas}</tbody></table>
    </div>
    <div class="no-print" style="text-align:center"><button class="btn" onclick="window.print()">🖨️ Imprimir hoja</button></div>"""
    components.html(html, height=min(260 + 46 * (n_filas + hoja["Pasillo"].nunique()), 4000), scrolling=True)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        out = hoja.drop(columns="Pasillo").assign(Check="")
        out.to_excel(xw, sheet_name="Reposición", index=False, startrow=2)
        ws, wb = xw.sheets["Reposición"], xw.book
        ws.write(0, 0, f"REPOSICIÓN ZM_CAJAS · {emision} · {n_sku} SKUs · {n_cajas} cajas",
                 wb.add_format({"bold": True, "font_size": 13}))
        head = wb.add_format({"bold": True, "bg_color": "#1E293B", "font_color": "white", "border": 1})
        for j, (c, w) in enumerate(zip(out.columns, [14, 42, 22, 18, 10, 10])):
            ws.write(2, j, c, head)
            ws.set_column(j, j, w)
        ws.set_landscape(); ws.set_paper(9); ws.fit_to_pages(1, 0); ws.repeat_rows(2)
    st.download_button("⬇️ Descargar Excel", buf.getvalue(), file_name="reposicion_zm_cajas.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
