import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import motor

st.set_page_config(page_title="Escasez ZM_CAJA | CD Coquimbo", page_icon="🚚", layout="wide")
BASE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(BASE, "maestros")

st.markdown("""<style>
.block-container {padding-top: 1.5rem;}
.titulo {font-size: 1.8rem; font-weight: 700; margin-bottom: 0;}
.sub {color: #888; margin-top: 0;}
div[data-testid="stMetric"] {background: rgba(59,130,246,0.10); border-radius: 10px;
                             padding: 10px 14px; border-left: 5px solid #3B82F6;}
</style>""", unsafe_allow_html=True)
st.markdown('<p class="titulo">🚚 Escasez proyectada ZM_CAJA</p>', unsafe_allow_html=True)
st.markdown('<p class="sub">CD Coquimbo · Venta completa del fleetloader vs stock en picking de cajas · '
            'abastecer antes de liberar las rutas</p>', unsafe_allow_html=True)


@st.cache_data
def maestros():
    return motor.cargar_maestros(os.path.join(M, "ubicaciones.csv"), os.path.join(M, "preferencia.csv"),
                                 os.path.join(M, "zonas.csv"))


@st.cache_data
def paletizado():
    return motor.cargar_paletizado(os.path.join(M, "paletizado.csv"))


@st.cache_data(ttl=3600)
def bbdd():
    return motor.cargar_bbdd(os.path.join(M, "bbdd_codigos.csv"))


# ------------------------------------------------------------------ barra lateral
with st.sidebar:
    st.header("1. Archivos")
    f_fleet = st.file_uploader("Fleetloader Truck (FLETBxxxx.TXT)", type=["txt", "csv", "xlsx", "xls"],
                               accept_multiple_files=True,
                               help="Puedes subir más de uno si la venta del día viene en varias sesiones.")
    f_vu = st.file_uploader("Operaciones de vida útil (.csv)", type=["csv"])

if not (f_fleet and f_vu):
    st.info("⬅️ Sube el **fleetloader** y las **operaciones de vida útil**.")
    with st.expander("¿Cómo calcula?", expanded=True):
        st.markdown("""
1. **Fleetloader → SAP:** cada `SKU` de Truck se convierte a SAP con `bbdd_codigos.csv`
   (la misma BBDD del Sistema de Revisión Venta). Se suma el mismo SKU repetido en varias bahías.
2. **Tabla dinámica:** cajas totales por SKU SAP de todas las rutas, y matriz SKU × ruta en orden `LOAD_PRIORITY`.
3. **Cruce con vida útil:** stock en estado D de la posición **ZM_CAJ** asignada al SKU (preferencias).
4. **Necesidad = venta − stock picking.**
   - **Ola 1:** lo que cabe ahora (capacidad − stock). Se repone **antes de liberar**.
   - **Llenados extra:** cuántas veces más hay que llenar la posición si es más chica que la venta.
5. **Ruta donde empieza la escasez:** recorriendo las rutas por `LOAD_PRIORITY`, la primera que deja la posición
   en cero con el stock de hoy, y la primera que lo haría aun llenando la posición al 100 %.
6. **Quiebre CD:** ALMAC/ALMPIC no alcanza para la necesidad (pedir traslado / revisar CL).

Los SKU que no tienen posición ZM_CAJA (ZM_PICK / ZM_PALLET) quedan fuera: van por trabajo dirigido del WMS.
""")
    st.stop()

# ------------------------------------------------------------------ lectura fleetloader
try:
    partes = [motor.leer_fleetloader(f) for f in f_fleet]
    raw = pd.concat([p[0] for p in partes], ignore_index=True)
    cols = partes[0][1]
except Exception as e:
    st.error(f"No se pudo leer el fleetloader: {e}")
    st.stop()

if not all(cols.get(k) for k in ["camion", "codigo", "cantidad"]):
    st.warning("No reconocí todas las columnas del fleetloader. Indícalas:")
    c = list(raw.columns)
    a, b, d = st.columns(3)
    cols["camion"] = a.selectbox("Ruta / camión", c)
    cols["codigo"] = b.selectbox("SKU TRUCK", c)
    cols["cantidad"] = d.selectbox("Cantidad (cajas)", c)

ub, pr, zm = maestros()
pal = paletizado()
bb, origen_bbdd = bbdd()
todas = motor.preparar_fleet(raw, cols, bb, pal)
lista_rutas = motor.rutas(todas)
etiqueta = {r.camion: f"{r.orden_camion:02d} · {r.camion}" for r in lista_rutas.itertuples()}

with st.sidebar:
    st.header("2. Rutas")
    excluir = st.multiselect("Rutas ya liberadas (excluir)", list(etiqueta), format_func=etiqueta.get,
                             help="Si ya liberaste algunas rutas y su picking está descontado del stock, sácalas.")
    st.header("3. Parámetros")
    descontar = st.checkbox("Descontar pallets completos", value=False,
                            help="Si una ruta lleva ≥ 1 pallet completo de un SKU, esas cajas salen por trabajo "
                                 "dirigido y no desde ZM_CAJA. Norma: paletizado WMS; si no está, la del Truck "
                                 "(QTY_PER_LAYER × LAYERS_PER_PALLET). Apagado = escenario conservador.")
    holgura = st.number_input("Holgura sobre la venta (%)", 0, 50, 0)
    tol = st.number_input("Tolerancia FEFO (días)", 0, 60, 0)
    st.caption("Origen ALMAC / ALMPIC · estado D · sin vencidos · FEFO y restos primero.")

# ------------------------------------------------------------------ cálculo
try:
    fleet = motor.preparar_fleet(raw, cols, bb, pal, descontar_pallets=descontar, excluir_rutas=excluir)
    vu = motor.cargar_vida_util(f_vu)
    fecha_ref = datetime.now(ZoneInfo("America/Santiago")).date()
    resumen, movs, sin_zm, sin_map, alertas = motor.calcular(
        fleet, vu, ub, pr, zm, pal, fecha_ref=fecha_ref, tolerancia_dias=int(tol), holgura_pct=float(holgura))
    dinamica = motor.tabla_dinamica(fleet)
except Exception as e:
    st.error(f"No se pudo procesar: {e}")
    st.stop()

n = lambda x: f"{int(x):,}".replace(",", ".")  # noqa: E731
rep = resumen[resumen["necesidad"] > 0]
fecha_sesion = ", ".join(sorted(raw[cols["fecha"]].dropna().unique())) if cols.get("fecha") else "—"
k = st.columns(6)
k[0].metric("Rutas", n(fleet["camion"].nunique()))
k[1].metric("Cajas fleetloader", n(fleet["cajas_pedido"].sum()))
k[2].metric("Cajas desde ZM_CAJA", n(resumen["venta_cajas"].sum()))
k[3].metric("SKU a reponer", n(len(rep)))
k[4].metric("Cajas a reponer", n(rep["necesidad"].sum()), help=f"Ola 1 (cabe ahora): {n(rep['reponer_ola1'].sum())} cj")
k[5].metric("SKU quiebre CD", n((resumen["faltante_cd"] > 0).sum()))
st.caption(f"Sesión Truck: {fecha_sesion} · BBDD códigos: {origen_bbdd} · Fecha de referencia {fecha_ref:%d-%m-%Y}")
if len(sin_map):
    st.warning(f"⚠️ {len(sin_map)} SKU TRUCK sin mapeo a SAP ({n(sin_map['cajas'].sum())} cj). "
               "Agrégalos a bbdd_codigos.csv (pestaña *Sin mapeo / sin ZM_CAJA*).")


def pintar(row):
    return [f"background-color: {motor.ESTADOS.get(row['estado'], 'white')}; color: #111"] * len(row)


t1, t2, t3, t4, t5, t6 = st.tabs(["📋 Resumen por SKU", "🖨️ Hoja operarios", "🔁 Movimientos",
                                  "📊 Tabla dinámica / rutas", "❓ Sin mapeo / sin ZM_CAJA", "⚠️ Alertas"])
with t1:
    est = st.multiselect("Estado", list(motor.ESTADOS), default=list(motor.ESTADOS)[:3])
    vista = resumen[resumen["estado"].isin(est)] if est else resumen
    st.dataframe(vista.style.apply(pintar, axis=1), use_container_width=True, height=560, hide_index=True,
                 column_config={"venta_cajas": "Venta (cj)", "stock_picking": "Stock picking",
                                "reponer_ola1": "Ola 1 (cabe ahora)", "reponer_intermedio": "Reposición intermedia",
                                "llenados_extra": "Llenados extra", "faltante_cd": "Faltante CD",
                                "ruta_escasez_hoy": "Escasez desde ruta (hoy)",
                                "ruta_escasez_lleno": "Escasez desde ruta (posición llena)"})

with t2:
    if movs.empty:
        st.success("No hay cajas que reponer: el stock de picking cubre la venta. 🎉")
    else:
        a, b = st.columns([1, 2])
        olas = a.multiselect("Ola", sorted(movs["ola"].unique()), default=[1],
                             help="Ola 1 = llenar ahora. Olas 2+ = reposiciones durante el picking.")
        pas = b.multiselect("Pasillo destino", sorted(movs["pasillo_destino"].unique()), placeholder="Todos")
        sel = movs[movs["ola"].isin(olas)] if olas else movs
        sel = sel[sel["pasillo_destino"].isin(pas)] if pas else sel
        hoja = motor.hoja_operarios(sel)
        if hoja.empty:
            st.info("Sin tareas con esos filtros.")
        else:
            fh = datetime.now(ZoneInfo("America/Santiago")).strftime("%d-%m-%Y %H:%M")
            filtros = f"Ola: {', '.join(map(str, olas)) or 'Todas'} | Pasillos: {', '.join(pas) or 'Todos'}"
            html = motor.html_hoja_ruta(hoja, fh, filtros)
            components.html(f"""{html}<div class="no-print" style="text-align:center;margin-top:12px">
              <button onclick="window.print()" style="background:#2563EB;color:#fff;font-weight:bold;padding:12px 24px;
              font-size:16px;border:none;border-radius:6px;cursor:pointer">🖨️ Imprimir</button></div>""",
                            height=800, scrolling=True)
            c1, c2 = st.columns(2)
            c1.download_button("⬇️ Hoja operarios (HTML)", f"<html><head><meta charset='utf-8'></head><body>{html}</body></html>",
                               file_name="hoja_escasez_zm_caja.html", mime="text/html", use_container_width=True)
            c2.download_button("⬇️ Hoja operarios (Excel)", motor.a_excel_hoja(hoja, f"Reabasto ZM_CAJA · {fh} · {filtros}"),
                               file_name="hoja_escasez_zm_caja.xlsx", use_container_width=True,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
with t3:
    st.dataframe(movs, use_container_width=True, height=560, hide_index=True)
with t4:
    st.markdown("**Venta completa por SKU (TRUCK → SAP)**")
    st.dataframe(dinamica, use_container_width=True, height=320, hide_index=True)
    st.markdown("**Cajas desde ZM_CAJA por ruta (orden LOAD_PRIORITY)** — solo SKU con posición ZM_CAJA")
    st.dataframe(motor.matriz_camiones(fleet, solo=resumen["sku_sap"]), use_container_width=True, height=360,
                 hide_index=True)
with t5:
    st.markdown("**SKU TRUCK sin mapeo a SAP**")
    st.dataframe(sin_map, use_container_width=True, hide_index=True)
    st.markdown("**SKU sin posición ZM_CAJA** (pallet / trabajo dirigido u otra zona)")
    st.dataframe(sin_zm, use_container_width=True, hide_index=True)
with t6:
    st.dataframe(alertas, use_container_width=True, height=500, hide_index=True)

st.divider()
params = {"Fecha de referencia": fecha_ref.strftime("%d-%m-%Y"), "Sesión Truck": fecha_sesion,
          "Fleetloader": ", ".join(f.name for f in f_fleet), "Vida útil": f_vu.name, "BBDD códigos": origen_bbdd,
          "Rutas excluidas": ", ".join(excluir) or "Ninguna",
          "Descontar pallets completos": "Sí" if descontar else "No", "Holgura (%)": holgura,
          "Tolerancia FEFO (días)": int(tol)}
st.download_button("📊 Reporte completo (Excel)", type="primary", use_container_width=True,
                   data=motor.a_excel({"Resumen SKU": resumen, "Movimientos": movs, "Venta por SKU": dinamica,
                                       "Venta ZM_CAJA por ruta": motor.matriz_camiones(fleet, solo=resumen["sku_sap"]),
                                       "Detalle fleetloader": fleet, "Sin mapeo": sin_map, "Sin ZM_CAJA": sin_zm,
                                       "Alertas": alertas}, params),
                   file_name=f"escasez_zm_caja_{fecha_ref:%Y%m%d}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
