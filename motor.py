"""
Motor · Escasez proyectada ZM_CAJA (CD Coquimbo)
=================================================
Objetivo: antes de liberar los camiones, saber cuántas cajas faltarán en cada
posición de picking ZM_CAJ para cubrir TODA la venta del fleetloader, y no ir
eliminando la escasez del WMS camión por camión.

Flujo
1. Fleetloader Truck (TXT separado por '|')  ->  cajas por ruta y SKU TRUCK.
2. SKU TRUCK -> SKU SAP con bbdd_codigos.csv (misma BBDD del Sistema de Revisión Venta).
3. Tabla dinámica: venta total por SKU SAP y matriz SKU x ruta (orden LOAD_PRIORITY).
4. Cruce con Operaciones de vida útil: stock (estado D) en la posición ZM_CAJ del SKU.
5. Necesidad = venta - stock picking, repartida en olas:
     ola 1   = lo que cabe ahora (capacidad - stock), se hace ANTES de liberar;
     ola 2.. = reposiciones intermedias cuando la posición es más chica que la venta.
6. Origen: ALMAC, ALMPIC, MIX-01, REBA-01 y STREC (opcional: la ZM_PICK del mismo SKU), estado D, no vencidos.
   Se excluyen reempaque, subestándar, revisión, consumo, merma, retenido y transferencias. FEFO 100 % (sin tolerancia).
   A igual vencimiento: restos en 2°/3° nivel primero (menos cajas primero), luego restos en piso,
   luego pallets completos en piso y al final pallets completos en altura.
   Si un resto en altura cabe entero en el espacio libre de la posición, se baja completo.
7. Primera ruta que caería en escasez (con el stock de hoy y con la posición llena).

ZM_PICK / ZM_PALLET no se calculan aquí: van por trabajo dirigido del WMS.
"""
import io
import math
import re
import unicodedata
import urllib.request
from html import escape

import pandas as pd

AREAS_ORIGEN = ["ALMAC", "ALMPIC"]                    # almacenamiento y almacenamiento picking
UBIC_ORIGEN_EXTRA = r"^(MIX-01|REBA-01|STREC\d*)$"    # ubicaciones de patio válidas como origen
# NO se consideran: reempaque (REEM), subestándar (SUBEST), revisión/consumo (REV), merma (COBR),
# retenido (RETE), transferencias (RDTS...), retorno flete y el resto del patio.


def es_origen(vu):
    """True para stock que sirve para abastecer: ALMAC, ALMPIC, MIX-01, REBA-01 y STREC01..n."""
    return vu["Area"].isin(AREAS_ORIGEN) | vu["Ubicacion"].str.upper().str.match(UBIC_ORIGEN_EXTRA, na=False)
URL_BBDD = "https://raw.githubusercontent.com/ebcgon10/sistema-revision-venta/main/bbdd_codigos.csv"

# columnas del fleetloader Truck (FLETBxxxx.TXT)
COLS_TRUCK = {
    "camion": "ROUTE_ID", "orden": "LOAD_PRIORITY", "codigo": "SKU", "cantidad": "QUANTITY",
    "descripcion": "TSDBA_TS_SKU_DESCRIPTION", "zona_truck": "WAREHOUSE_ZONE_ID", "bahia": "BAY_NUMBER",
    "cajas_capa": "QTY_PER_LAYER", "capas": "LAYERS_PER_PALLET", "fecha": "SESSION_DATE",
}
# para otros formatos (Excel/CSV) se intenta reconocer por nombre
CLAVES = {
    "camion": ["route id", "ruta", "camion", "patente", "vehiculo", "truck", "carga", "viaje"],
    "codigo": ["sku", "codigo truck", "codigo", "producto", "material", "articulo"],
    "cantidad": ["quantity", "cajas", "cantidad", "cant", "bultos"],
    "descripcion": ["tsdba ts sku description", "descripcion", "nombre", "glosa"],
    "orden": ["load priority", "route number", "orden", "secuencia", "prioridad"],
}


# ================================================================== utilidades
def _norm_cols(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _txt(s):
    return s.astype(str).str.strip().replace({"nan": "", "None": "", "NaN": ""})


def _num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", ".", regex=False), errors="coerce").fillna(0)


def _bytes(archivo):
    if hasattr(archivo, "seek"):
        archivo.seek(0)
    return archivo.read() if hasattr(archivo, "read") else open(archivo, "rb").read()


def _decodificar(raw):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _leer_csv(archivo, sep=None):
    texto = _decodificar(_bytes(archivo))
    if sep is None:
        primera = texto.splitlines()[0]
        sep = max(["|", ";", ",", "\t"], key=primera.count)
    return _norm_cols(pd.read_csv(io.StringIO(texto), sep=sep, dtype=str, index_col=False))


def _sin_tildes(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def norm_codigo(s):
    """'00450604', '450604' y '450604.0' quedan iguales; 'ET8708371' no cambia."""
    s = _txt(s).str.upper().str.replace(r"\.0$", "", regex=True)
    return s.str.lstrip("0").replace({"": "0"})


def nivel_de(ubic):
    p = str(ubic).split("-")
    return p[2] if len(p) >= 3 else ""


def pasillo_de(ubic):
    return str(ubic).split("-")[0]


# ================================================================== maestros WMS
def _df(x, sep=","):
    return _norm_cols(x.astype(str)) if isinstance(x, pd.DataFrame) else _leer_csv(x, sep=sep)


def cargar_maestros(ruta_ubic, ruta_pref, ruta_zonas):
    """Acepta rutas CSV o DataFrames (p.ej. hojas del Excel 'Datos para armar')."""
    ubic = _df(ruta_ubic).rename(columns={
        "NOMBRE UBICACIÓN": "ubicacion", "AREA": "area_maestro", "CAPACIDAD MAXIMA": "capacidad_txt"})
    ubic["ubicacion"] = _txt(ubic["ubicacion"])
    ubic["capacidad"] = ubic["capacidad_txt"].astype(str).str.extract(r"(\d+)")[0].astype(float)
    ubic["unidad_cap"] = ubic["capacidad_txt"].astype(str).str.extract(r"\d+\s*(\S+)")[0]
    pref = _df(ruta_pref).rename(columns={
        "Ubicación": "ubicacion", "Número de artículo": "articulo", "Secuencia": "secuencia"})
    pref["ubicacion"] = _txt(pref["ubicacion"])
    pref["articulo"] = _txt(pref["articulo"])
    pref = pref[(pref["ubicacion"] != "") & (pref["articulo"] != "")].drop_duplicates(["ubicacion", "articulo"])
    if "secuencia" in pref.columns:
        pref["secuencia"] = pd.to_numeric(pref["secuencia"], errors="coerce")
    else:
        pref["secuencia"] = float("nan")
    # sin secuencia de ruta en el maestro: se ordena por ubicación (pasillo-columna-nivel-posición)
    orden_ubic = pref["ubicacion"].rank(method="dense")
    pref["secuencia"] = pref["secuencia"].fillna(orden_ubic + 100000)
    zonas = _df(ruta_zonas).rename(columns={
        "UBICACIÓN": "ubicacion", "ZONA DE TRABAJO": "zona_trabajo",
        "ZONA DE MOVIMIENTO": "zona_movimiento", "ZONA DE RECOGIDA": "zona_recogida"})
    for c in zonas.columns:
        zonas[c] = _txt(zonas[c])
    zonas["zona_movimiento"] = zonas["zona_movimiento"].str.upper()
    return ubic, pref, zonas


def hojas_maestro_excel(archivo):
    """Del Excel 'Datos para armar' toma las hojas de UBICACIONES, PREFERENCIA y ZM (por nombre parecido)."""
    if hasattr(archivo, "seek"):
        archivo.seek(0)
    hojas = pd.read_excel(archivo, sheet_name=None, dtype=str)
    buscar = lambda clave: next((df for n, df in hojas.items() if clave in _sin_tildes(n)), None)  # noqa: E731
    u, p, z = buscar("ubicacion"), buscar("preferencia"), buscar("zm") if buscar("zm") is not None else buscar("zona")
    faltan = [n for n, d in [("UBICACIONES", u), ("PREFERENCIA", p), ("ZM", z)] if d is None]
    if faltan:
        raise ValueError(f"Al Excel de maestros le faltan las hojas: {', '.join(faltan)}")
    return u, p, z


def cargar_paletizado(archivo):
    nombre = getattr(archivo, "name", str(archivo)).lower()
    df = _norm_cols(pd.read_excel(archivo, dtype=str)) if nombre.endswith((".xlsx", ".xls")) else _leer_csv(archivo)
    df = df.rename(columns={"Numero de material": "articulo", "Número de material": "articulo",
                            "Cajas por Pallet": "cajas_por_pallet"})
    df["articulo"] = _txt(df["articulo"])
    df["cajas_por_pallet"] = _num(df["cajas_por_pallet"])
    return df[df["cajas_por_pallet"] > 0].groupby("articulo", as_index=False)["cajas_por_pallet"].max()


def cargar_bbdd(ruta_local, intentar_github=True):
    """Primero la BBDD vigente del repo sistema-revision-venta (una sola fuente de verdad);
    si no hay red, la copia local de maestros/."""
    df, origen = None, "copia local (maestros/bbdd_codigos.csv)"
    if intentar_github:
        try:
            with urllib.request.urlopen(URL_BBDD, timeout=6) as r:
                df = pd.read_csv(io.BytesIO(r.read()), dtype=str)
                origen = "GitHub sistema-revision-venta (vigente)"
        except Exception:
            df = None
    if df is None:
        df = pd.read_csv(ruta_local, dtype=str)
    df["CODIGO_TRK"] = _txt(df["CODIGO_TRK"])
    df["CODIGO_SAP"] = _txt(df["CODIGO_SAP"])
    df["trk_norm"] = norm_codigo(df["CODIGO_TRK"])
    return df.drop_duplicates("trk_norm"), origen


# ================================================================== vida útil
def cargar_vida_util(archivo):
    df = _leer_csv(archivo)
    req = {"Area", "Ubicacion", "LPN", "Articulo", "Descripcion", "Cantidad", "Estado de inventario",
           "Fecha de caducidad"}
    faltan = req - set(df.columns)
    if faltan:
        raise ValueError(f"A operaciones de vida útil le faltan columnas: {', '.join(sorted(faltan))}")
    for c in ["Area", "Ubicacion", "LPN", "Articulo", "Descripcion", "Estado de inventario"]:
        df[c] = _txt(df[c])
    df["Cantidad"] = _num(df["Cantidad"])
    df["Fecha de caducidad"] = pd.to_datetime(df["Fecha de caducidad"], dayfirst=True, errors="coerce")
    return df


# ================================================================== fleetloader
def leer_fleetloader(archivo):
    """Lee el TXT de Truck (o un Excel/CSV) y devuelve (df, columnas_detectadas)."""
    nombre = getattr(archivo, "name", str(archivo)).lower()
    if nombre.endswith((".xlsx", ".xls", ".xlsm")):
        if hasattr(archivo, "seek"):
            archivo.seek(0)
        df = _norm_cols(pd.read_excel(archivo, dtype=str))
    else:
        df = _leer_csv(archivo)
    df = df.dropna(axis=1, how="all")  # el TXT trae 23 encabezados pero 17 datos por línea
    return df, detectar_columnas(df)


def detectar_columnas(df):
    if {COLS_TRUCK["camion"], COLS_TRUCK["codigo"], COLS_TRUCK["cantidad"]} <= set(df.columns):
        return {k: (v if v in df.columns else None) for k, v in COLS_TRUCK.items()}
    cols = {c: _sin_tildes(c) for c in df.columns}
    out, usadas = {}, set()
    for campo in ["cantidad", "codigo", "camion", "descripcion", "orden"]:
        elegido = next((c for clave in CLAVES[campo] for c, cn in cols.items()
                        if c not in usadas and (cn == clave or (" " + clave + " ") in (" " + cn + " "))), None)
        out[campo] = elegido
        if elegido:
            usadas.add(elegido)
    for k in ["zona_truck", "bahia", "cajas_capa", "capas", "fecha"]:
        out[k] = None
    return out


def preparar_fleet(df, cols, bbdd, paletizado=None, descontar_pallets=False, excluir_rutas=None):
    """Una fila por ruta + SKU con código SAP, cajas pedidas y cajas que salen del picking."""
    g = lambda k: _txt(df[cols[k]]) if cols.get(k) else ""  # noqa: E731
    d = pd.DataFrame({"camion": g("camion") if cols.get("camion") else "SIN RUTA",
                      "sku_truck": g("codigo"), "descripcion_truck": g("descripcion"),
                      "zona_truck": g("zona_truck"), "cajas": _num(df[cols["cantidad"]])})
    d["camion"] = d["camion"].replace({"": "SIN RUTA"})
    if cols.get("orden"):
        d["orden_camion"] = pd.to_numeric(df[cols["orden"]], errors="coerce")
    else:
        d["orden_camion"] = d["camion"].map({c: i + 1 for i, c in enumerate(pd.unique(d["camion"]))})
    d["orden_camion"] = d["orden_camion"].fillna(d["orden_camion"].max() + 1 if d["orden_camion"].notna().any() else 1)
    if cols.get("cajas_capa") and cols.get("capas"):
        d["norma_truck"] = _num(df[cols["cajas_capa"]]) * _num(df[cols["capas"]])
    else:
        d["norma_truck"] = 0
    d = d[(d["sku_truck"] != "") & (d["cajas"] > 0)]
    if excluir_rutas:
        d = d[~d["camion"].isin(excluir_rutas)]

    d["trk_norm"] = norm_codigo(d["sku_truck"])
    d = d.merge(bbdd[["trk_norm", "CODIGO_SAP"]], on="trk_norm", how="left")
    sap = dict(zip(norm_codigo(bbdd["CODIGO_SAP"]), bbdd["CODIGO_SAP"]))  # por si ya viene en SAP
    falta = d["CODIGO_SAP"].isna() & d["trk_norm"].isin(sap)
    d.loc[falta, "CODIGO_SAP"] = d.loc[falta, "trk_norm"].map(sap)
    d["sku_sap"] = d["CODIGO_SAP"].fillna("SIN MAPEO")

    # el TXT trae el mismo SKU repetido por bahía: se suma por ruta
    f = (d.groupby(["orden_camion", "camion", "sku_truck", "sku_sap"], as_index=False)
         .agg(descripcion_truck=("descripcion_truck", "first"), zona_truck=("zona_truck", "first"),
              bahias=("cajas", "size"), cajas_pedido=("cajas", "sum"), norma_truck=("norma_truck", "max")))

    norma = (paletizado.set_index("articulo")["cajas_por_pallet"]
             if paletizado is not None and not paletizado.empty else pd.Series(dtype=float))
    f["norma_pallet"] = f["sku_sap"].map(norma)
    f["fuente_norma"] = f["norma_pallet"].notna().map({True: "WMS", False: "Truck"})
    f["norma_pallet"] = f["norma_pallet"].fillna(f["norma_truck"]).fillna(0)
    f["pallets_completos"] = 0
    if descontar_pallets:
        ok = f["norma_pallet"] > 0
        f.loc[ok, "pallets_completos"] = (f.loc[ok, "cajas_pedido"] // f.loc[ok, "norma_pallet"]).astype(int)
    f["cajas_picking"] = f["cajas_pedido"] - f["pallets_completos"] * f["norma_pallet"]
    f["orden_camion"] = f["orden_camion"].astype(int)
    return f.sort_values(["orden_camion", "sku_sap"]).reset_index(drop=True)


def rutas(fleet):
    return fleet.drop_duplicates("camion").sort_values("orden_camion")[["orden_camion", "camion"]]


def tabla_dinamica(fleet):
    t = (fleet.groupby("sku_sap", as_index=False)
         .agg(sku_truck=("sku_truck", lambda s: ", ".join(sorted(set(s)))),
              descripcion=("descripcion_truck", "first"), zona_truck=("zona_truck", "first"),
              rutas=("camion", "nunique"), cajas_pedido=("cajas_pedido", "sum"),
              pallets_completos=("pallets_completos", "sum"), cajas_picking=("cajas_picking", "sum")))
    return t.sort_values("cajas_pedido", ascending=False).reset_index(drop=True)


def matriz_camiones(fleet, solo=None):
    f = fleet if solo is None else fleet[fleet["sku_sap"].isin(solo)]
    if f.empty:
        return pd.DataFrame()
    m = f.pivot_table(index=["sku_sap", "descripcion_truck"], columns=["orden_camion", "camion"],
                      values="cajas_picking", aggfunc="sum", fill_value=0)
    m.columns = [f"{o:02d} · {c}" for o, c in m.columns]
    m["TOTAL"] = m.sum(axis=1)
    return m.sort_values("TOTAL", ascending=False).reset_index()


# ================================================================== cálculo principal
ESTADOS = {"Quiebre CD (no alcanza almacenamiento)": "#F4CCCC",
           "Reponer + reposición intermedia": "#FCE5CD",
           "Reponer antes de liberar": "#FFF2CC",
           "Cubierto con stock picking": "#D9EAD3"}


def calcular(fleet, vu, ubic, pref, zonas, paletizado=None, fecha_ref=None, holgura_pct=0.0,
             bajar_restos=True, usar_zm_pick=False):
    fecha_ref = pd.Timestamp(fecha_ref or pd.Timestamp.today().normalize())
    alertas = []

    # posiciones ZM_CAJ con artículo asignado (1 por artículo)
    pos = zonas[zonas["zona_movimiento"] == "ZM_CAJ"][["ubicacion", "zona_trabajo"]].drop_duplicates("ubicacion")
    pos = pos.merge(pref[["ubicacion", "articulo", "secuencia"]], on="ubicacion")
    pos = pos[pos["articulo"] != ""].merge(ubic[["ubicacion", "capacidad", "unidad_cap"]], on="ubicacion", how="left")
    for u_, g_ in pos.groupby("ubicacion"):
        if len(g_) > 1:
            alertas.append({"tipo": "Ubicación ZM_CAJA con 2+ artículos en preferencia (capacidad compartida)",
                            "ubicacion": u_, "articulo": ", ".join(g_["articulo"]),
                            "detalle": "Se calcula cada artículo con la capacidad completa → revisar vía ADC"})
    for _, r in pos[pos["unidad_cap"].fillna("").str.lower().str.startswith("pal")].iterrows():
        alertas.append({"tipo": "Posición ZM_CAJA con capacidad en Palés (se lee como cajas)", "ubicacion": r["ubicacion"],
                        "articulo": r["articulo"], "detalle": f"Capacidad {int(r['capacidad'])} Palés → revisar vía ADC"})
    pos["capacidad"] = pos["capacidad"].fillna(0)
    pos = pos.sort_values("secuencia").drop_duplicates("articulo")

    venta = fleet[fleet["sku_sap"] != "SIN MAPEO"].groupby("sku_sap", as_index=False).agg(
        venta_cajas=("cajas_picking", "sum"), rutas=("camion", "nunique"))
    venta = venta[venta["venta_cajas"] > 0]

    vu = vu.copy()
    vu["dias_vida"] = (vu["Fecha de caducidad"] - fecha_ref).dt.days
    disp = vu[vu["Estado de inventario"] == "D"]
    st_pick = (disp.groupby(["Ubicacion", "Articulo"], as_index=False)["Cantidad"].sum()
               .rename(columns={"Ubicacion": "ubicacion", "Articulo": "articulo", "Cantidad": "stock_picking"}))
    for _, r in vu[(vu["Estado de inventario"] != "D") & vu["Ubicacion"].isin(pos["ubicacion"])].iterrows():
        alertas.append({"tipo": "Stock en ZM_CAJA con estado distinto de D (no se cuenta)", "ubicacion": r["Ubicacion"],
                        "articulo": r["Articulo"], "detalle": f"{int(r['Cantidad'])} cj estado {r['Estado de inventario']}"})
    # posición ZM_CAJ con otro artículo
    mezcla = disp.merge(pos[["ubicacion", "articulo"]], left_on="Ubicacion", right_on="ubicacion")
    for _, r in mezcla[mezcla["Articulo"] != mezcla["articulo"]].iterrows():
        alertas.append({"tipo": "Posición ZM_CAJA con otro artículo", "ubicacion": r["Ubicacion"],
                        "articulo": r["articulo"], "detalle": f"Encontrado {r['Articulo']} ({int(r['Cantidad'])} cj)"})

    # posiciones ZM_PICK / ZM_PALLET (pallet en piso) asignadas al MISMO artículo: también sirven de origen
    pick_pal = zonas[zonas["zona_movimiento"].isin(["ZM_PICK", "ZM_PALLET"])][["ubicacion"]]
    pick_pal = pick_pal.merge(pref[["ubicacion", "articulo"]], on="ubicacion")
    llave_pp = set(zip(pick_pal["ubicacion"], pick_pal["articulo"]))
    es_pp = pd.Series([(u, a) in llave_pp for u, a in zip(disp["Ubicacion"], disp["Articulo"])], index=disp.index)
    origen = disp[es_origen(disp) | (es_pp & usar_zm_pick)].copy()
    origen.loc[~es_origen(origen), "Area"] = "ZM_PICK propia"
    st_pp = disp[es_pp].groupby("Articulo")["Cantidad"].sum()
    vencidos = origen[origen["dias_vida"] <= 0]
    origen = origen[(origen["dias_vida"] > 0) & origen["Fecha de caducidad"].notna()]
    st_alm = origen.groupby("Articulo")["Cantidad"].sum()

    t = venta.merge(pos, left_on="sku_sap", right_on="articulo", how="left")
    sin_zm = t[t["ubicacion"].isna()][["sku_sap", "venta_cajas", "rutas"]].copy()
    t = t[t["ubicacion"].notna()].merge(st_pick, on=["ubicacion", "articulo"], how="left")
    t["stock_picking"] = t["stock_picking"].fillna(0)
    t["stock_almacenamiento"] = t["articulo"].map(st_alm).fillna(0)
    t["stock_zm_pick"] = t["articulo"].map(st_pp).fillna(0)

    # si el maestro dice menos capacidad que lo que hoy hay físicamente, se usa el stock como capacidad
    sobre = t[t["stock_picking"] > t["capacidad"]]
    for _, r in sobre.iterrows():
        alertas.append({"tipo": "Capacidad del maestro menor al stock actual (se usa el stock)",
                        "ubicacion": r["ubicacion"], "articulo": r["articulo"],
                        "detalle": f"Maestro {int(r['capacidad'])} cj · stock {int(r['stock_picking'])} cj → revisar vía ADC"})
    t["capacidad"] = t[["capacidad", "stock_picking"]].max(axis=1)

    t["venta_objetivo"] = (t["venta_cajas"] * (1 + holgura_pct / 100)).apply(math.ceil)
    t["necesidad"] = (t["venta_objetivo"] - t["stock_picking"]).clip(lower=0)
    t["espacio_libre"] = (t["capacidad"] - t["stock_picking"]).clip(lower=0)
    t["reponer_ola1"] = t[["necesidad", "espacio_libre"]].min(axis=1)
    t["reponer_intermedio"] = t["necesidad"] - t["reponer_ola1"]
    t["llenados_extra"] = [math.ceil(r / c) if c > 0 and r > 0 else 0
                           for r, c in zip(t["reponer_intermedio"], t["capacidad"])]
    t["faltante_cd"] = (t["necesidad"] - t["stock_almacenamiento"]).clip(lower=0)

    # primera ruta en escasez recorriendo LOAD_PRIORITY
    f = fleet[fleet["sku_sap"].isin(t["articulo"])].sort_values("orden_camion").copy()
    f["acum"] = f.groupby("sku_sap")["cajas_picking"].cumsum()
    lim = t.set_index("articulo")
    f["hoy"] = f["sku_sap"].map(lim["stock_picking"])
    f["lleno"] = f["sku_sap"].map(lim[["stock_picking", "capacidad"]].max(axis=1))

    def primera(col):
        x = f[f["acum"] > f[col]].groupby("sku_sap").first()
        return x["orden_camion"].astype(str).str.zfill(2) + " · " + x["camion"]

    t["ruta_escasez_hoy"] = t["articulo"].map(primera("hoy")).fillna("—")
    t["ruta_escasez_lleno"] = t["articulo"].map(primera("lleno")).fillna("—")
    t["estado"] = [("Cubierto con stock picking" if n <= 0 else
                    "Quiebre CD (no alcanza almacenamiento)" if fc > 0 else
                    "Reponer + reposición intermedia" if ri > 0 else "Reponer antes de liberar")
                   for n, fc, ri in zip(t["necesidad"], t["faltante_cd"], t["reponer_intermedio"])]

    for _, r in t[(t["capacidad"] > 0) & (t["llenados_extra"] >= 3)].iterrows():
        alertas.append({"tipo": "Capacidad ZM_CAJA chica para la venta (evaluar cambio vía ADC)",
                        "ubicacion": r["ubicacion"], "articulo": r["articulo"],
                        "detalle": f"Venta {int(r['venta_cajas'])} cj vs capacidad {int(r['capacidad'])} cj "
                                   f"→ {int(r['llenados_extra'])} llenados extra"})
    for _, r in t[t["capacidad"] <= 0].iterrows():
        alertas.append({"tipo": "Posición ZM_CAJA sin capacidad en maestro", "ubicacion": r["ubicacion"],
                        "articulo": r["articulo"], "detalle": "Revisar maestro de ubicaciones"})

    desc = vu.drop_duplicates("Articulo").set_index("Articulo")["Descripcion"]
    desc_f = fleet.drop_duplicates("sku_sap").set_index("sku_sap")["descripcion_truck"]
    t["descripcion"] = t["articulo"].map(desc_f).fillna(t["articulo"].map(desc)).fillna("")
    t["pasillo"] = t["ubicacion"].map(pasillo_de)

    resumen = t[["secuencia", "pasillo", "ubicacion", "zona_trabajo", "articulo", "descripcion", "rutas",
                 "venta_cajas", "capacidad", "stock_picking", "necesidad", "reponer_ola1", "reponer_intermedio",
                 "llenados_extra", "stock_almacenamiento", "stock_zm_pick", "faltante_cd", "ruta_escasez_hoy",
                 "ruta_escasez_lleno", "estado"]].rename(columns={"ubicacion": "ubicacion_zm_caja",
                                                                  "articulo": "sku_sap"})
    orden = {e: i for i, e in enumerate(ESTADOS)}
    resumen = (resumen.assign(_o=resumen["estado"].map(orden))
               .sort_values(["_o", "necesidad"], ascending=[True, False]).drop(columns="_o").reset_index(drop=True))
    for c in ["venta_cajas", "capacidad", "stock_picking", "necesidad", "reponer_ola1", "reponer_intermedio",
              "stock_almacenamiento", "faltante_cd"]:
        resumen[c] = resumen[c].astype(int)

    # ---------- movimientos: FEFO 100 % y, a igual vencimiento, bajar restos en altura
    norma = (paletizado.set_index("articulo")["cajas_por_pallet"]
             if paletizado is not None and not paletizado.empty else pd.Series(dtype=float))
    origen["norma"] = origen["Articulo"].map(norma).fillna(origen["Articulo"].map(disp.groupby("Articulo")["Cantidad"].max()))
    origen["es_pallet"] = (origen["Cantidad"] >= origen["norma"]).astype(int)
    origen["nivel"] = origen["Ubicacion"].map(nivel_de)
    origen["en_altura"] = pd.to_numeric(origen["nivel"], errors="coerce").fillna(1) >= 2
    # prioridad dentro del MISMO vencimiento:
    # 0 resto en altura · 1 resto en piso · 2 pallet completo en piso · 3 pallet completo en altura
    origen["prioridad"] = [(0 if alt else 1) if not pal_ else (3 if alt else 2)
                           for pal_, alt in zip(origen["es_pallet"], origen["en_altura"])]
    origen["nivel_num"] = pd.to_numeric(origen["nivel"], errors="coerce").fillna(1)

    movs = []
    for _, d in t[(t["necesidad"] > 0) | bajar_restos].sort_values("secuencia").iterrows():
        cand = origen[origen["Articulo"] == d["articulo"]].copy()
        if cand.empty:
            continue
        # FEFO estricto: primero vence, primero sale. Desempate: restos en altura con menos cajas primero.
        cand["orden_cajas"] = [c if p < 2 else 0 for c, p in zip(cand["Cantidad"], cand["prioridad"])]
        cand["orden_nivel"] = [-n if p < 2 else n for n, p in zip(cand["nivel_num"], cand["prioridad"])]
        cand = cand.sort_values(["Fecha de caducidad", "prioridad", "orden_cajas", "orden_nivel"])
        falta, asig, ola1, cap = d["necesidad"], 0, d["reponer_ola1"], max(d["capacidad"], 1)
        espacio = d["espacio_libre"]
        for _, r in cand.iterrows():
            rest = r["Cantidad"]
            resto_alt = r["prioridad"] == 0
            if falta <= 0:
                # necesidad cubierta: solo se sigue si el siguiente LPN en orden FEFO es un resto
                # en altura que cabe ENTERO en el espacio libre de la posición (se baja y se elimina)
                if not (bajar_restos and resto_alt and 0 < rest <= espacio - asig):
                    break
                movs.append(_mov(1, d, r, rest, 0, "Baja resto en altura (espacio libre)"))
                asig += rest
                continue
            while rest > 0 and falta > 0:
                if asig < ola1:
                    ola, tope = 1, ola1 - asig
                else:
                    k = int((asig - ola1) // cap)
                    ola, tope = 2 + k, ola1 + (k + 1) * cap - asig
                tomar = min(rest, falta, tope)
                # si queda un resto en altura que cabe en la posición, se baja completo
                if bajar_restos and ola == 1 and resto_alt and 0 < rest - tomar <= espacio - asig - tomar:
                    tomar = rest
                queda = rest - tomar
                if r["es_pallet"]:
                    efecto = ("Mueve pallet completo" if queda == 0 else
                              "Abre pallet en altura" if r["en_altura"] else "Abre pallet en piso")
                elif r["en_altura"]:
                    efecto = "Elimina resto en altura" if queda == 0 else "Reduce resto en altura"
                else:
                    efecto = "Elimina resto en piso" if queda == 0 else "Saca de resto en piso"
                movs.append(_mov(ola, d, r, tomar, queda, efecto))
                rest -= tomar; falta -= tomar; asig += tomar
    movs = pd.DataFrame(movs)
    if not movs.empty:
        movs = movs.sort_values(["ola", "secuencia", "caducidad"]).reset_index(drop=True)
        movs.insert(0, "n_tarea", range(1, len(movs) + 1))
        movs = movs.drop(columns="secuencia")

    # control FEFO: stock en la posición de picking que vence DESPUÉS que lo más antiguo del almacenamiento
    cad_pick = (vu[(vu["Estado de inventario"] == "D")].merge(t[["ubicacion", "articulo"]],
                left_on=["Ubicacion", "Articulo"], right_on=["ubicacion", "articulo"]))
    min_alm = origen.groupby("Articulo")["Fecha de caducidad"].min()
    for _, r in cad_pick.iterrows():
        m_ = min_alm.get(r["Articulo"])
        if m_ is not None and pd.notna(r["Fecha de caducidad"]) and r["Fecha de caducidad"] > m_:
            alertas.append({"tipo": "FEFO: picking tiene lote más nuevo que el almacenamiento",
                            "ubicacion": r["Ubicacion"], "articulo": r["Articulo"],
                            "detalle": f"Picking vence {r['Fecha de caducidad']:%d-%m-%Y} · almacenamiento "
                                       f"tiene {m_:%d-%m-%Y} (se pickea primero lo más nuevo)"})

    sin_zm["motivo"] = "Sin posición ZM_CAJA (sale por pallet / trabajo dirigido u otra zona)"
    sin_map = (fleet[fleet["sku_sap"] == "SIN MAPEO"].groupby("sku_truck", as_index=False)
               .agg(descripcion=("descripcion_truck", "first"), cajas=("cajas_pedido", "sum"),
                    rutas=("camion", "nunique")))
    sin_map["motivo"] = "SKU TRUCK no está en bbdd_codigos.csv"
    arts = set(t["articulo"])
    for _, r in vencidos[vencidos["Articulo"].isin(arts)].iterrows():
        alertas.append({"tipo": "LPN vencido en almacenamiento (no se usa)", "ubicacion": r["Ubicacion"],
                        "articulo": r["Articulo"], "detalle": f"LPN {r['LPN']} ({int(r['Cantidad'])} cj)"})
    sin_alm = t[(t["necesidad"] > 0) & (t["stock_almacenamiento"] == 0) & t["articulo"].isin(vu["Articulo"])]
    for _, r in sin_alm.iterrows():
        alertas.append({"tipo": "Sin stock en almacenamiento ni MIX/REBA/STREC (pedir traslado / CL)", "ubicacion": r["ubicacion"],
                        "articulo": r["articulo"], "detalle": f"Necesidad {int(r['necesidad'])} cj"})
    alertas = pd.DataFrame(alertas, columns=["tipo", "ubicacion", "articulo", "detalle"])
    return resumen, movs, sin_zm.reset_index(drop=True), sin_map, alertas


def _mov(ola, d, r, tomar, queda, efecto):
    return {"ola": ola, "secuencia": d["secuencia"], "pasillo_destino": d["pasillo"],
            "ubicacion_origen": r["Ubicacion"], "area_origen": r["Area"], "nivel_origen": r["nivel"],
            "lpn": r["LPN"], "articulo": d["articulo"], "descripcion": d["descripcion"],
            "caducidad": r["Fecha de caducidad"], "dias_vida": int(r["dias_vida"]),
            "cajas_lpn": int(r["Cantidad"]), "norma_pallet": int(r["norma"]) if pd.notna(r["norma"]) else None,
            "tipo_lpn": "Pallet completo" if r["es_pallet"] else "Resto", "cajas_a_mover": int(tomar),
            "queda_en_lpn": int(queda), "efecto": efecto, "ubicacion_destino": d["ubicacion"],
            "zona_trabajo": d["zona_trabajo"], "ruta_escasez_hoy": d["ruta_escasez_hoy"],
            "tipo_tarea": "Baja resto" if efecto.startswith("Baja resto") else "Escasez"}


def verificar_fefo(movs, vu, fecha_ref=None):
    """Para cada SKU: ningún LPN de origen válido (D, no vencido) que vence ANTES que el último
    usado quedó sin tocar. Devuelve las violaciones (vacío = FEFO 100 %)."""
    if movs is None or movs.empty:
        return pd.DataFrame()
    fecha_ref = pd.Timestamp(fecha_ref or pd.Timestamp.today().normalize())
    ubic_pp = set(movs.loc[movs["area_origen"] == "ZM_PICK propia", "ubicacion_origen"])
    o = vu[(es_origen(vu) | vu["Ubicacion"].isin(ubic_pp)) & (vu["Estado de inventario"] == "D") &
           (vu["Fecha de caducidad"] > fecha_ref)]
    usados = movs.groupby(["articulo", "lpn"])["cajas_a_mover"].sum()
    malos = []
    for art, g in movs.groupby("articulo"):
        tope = g["caducidad"].max()
        previos = o[(o["Articulo"] == art) & (o["Fecha de caducidad"] < tope)]
        for _, r in previos.iterrows():
            if usados.get((art, r["LPN"]), 0) < r["Cantidad"]:
                malos.append({"articulo": art, "lpn_no_consumido": r["LPN"], "vence": r["Fecha de caducidad"],
                              "ultimo_usado_vence": tope})
    return pd.DataFrame(malos)


def propuesta_adc(alertas, ubic, pref):
    """Control de cambios (ADC) sugerido para el maestro WMS a partir de las alertas.
    Son propuestas: el key user valida cada línea antes de cargarla en el WMS."""
    cap = ubic.set_index("ubicacion")["capacidad_txt"]
    filas = []
    for _, r in alertas.iterrows():
        t, u = r["tipo"], r["ubicacion"]
        if t.startswith("Capacidad del maestro menor"):
            stock = re.search(r"stock (\d+)", r["detalle"]).group(1)
            filas.append(("Capacidad máxima", u, r["articulo"], cap.get(u, ""), f"≥ {stock} Piezas",
                          "Stock físico supera la capacidad configurada"))
        elif t.startswith("Capacidad ZM_CAJA chica"):
            filas.append(("Capacidad máxima / reubicación", u, r["articulo"], cap.get(u, ""),
                          "Aumentar capacidad o asignar posición más grande", r["detalle"]))
        elif t.startswith("Posición ZM_CAJA sin capacidad"):
            filas.append(("Capacidad máxima", u, r["articulo"], cap.get(u, ""), "Definir capacidad en Piezas",
                          "Capacidad 0 o vacía"))
        elif "capacidad en Palés" in t:
            filas.append(("Unidad de capacidad", u, r["articulo"], cap.get(u, ""), "Cambiar unidad a Piezas",
                          "Posición de cajas configurada en Palés"))
        elif t.startswith("Posición ZM_CAJA con otro artículo"):
            filas.append(("Preferencia / stock", u, r["articulo"], f"Preferencia {r['articulo']}",
                          "Reubicar el stock encontrado o actualizar la preferencia", r["detalle"]))
        elif t.startswith("Ubicación ZM_CAJA con 2+"):
            filas.append(("Preferencia", u, r["articulo"], "2 artículos en la misma ubicación",
                          "Dejar 1 artículo por posición ZM_CAJA", r["detalle"]))
    adc = pd.DataFrame(filas, columns=["tipo_cambio", "ubicacion", "articulo", "valor_actual", "valor_propuesto",
                                       "motivo"]).drop_duplicates(["tipo_cambio", "ubicacion", "articulo"])
    adc.insert(0, "n_adc", range(1, len(adc) + 1))
    for c in ["aprobado_por", "fecha_carga_wms", "estado"]:
        adc[c] = ""
    adc["estado"] = "Pendiente"
    return adc


# ================================================================== salidas
def hoja_operarios(movs):
    if movs is None or movs.empty:
        return pd.DataFrame()
    df = movs.sort_values(["tipo_tarea", "ola", "pasillo_destino", "ubicacion_destino"], ascending=[False, True, True, True])
    return pd.DataFrame({"N°": range(1, len(df) + 1),
                         "Ola": [str(o) if t == "Escasez" else "R" for o, t in zip(df["ola"], df["tipo_tarea"])], "Pasillo": df["pasillo_destino"].values,
                         "SKU": df["articulo"].values, "Descripción": df["descripcion"].values,
                         "Ubicación origen": df["ubicacion_origen"].values, "LPN": df["lpn"].values,
                         "Ubicación destino": df["ubicacion_destino"].values,
                         "Cajas": df["cajas_a_mover"].values, "Vence": pd.to_datetime(df["caducidad"]).dt.strftime("%d-%m-%Y").values,
                         "Efecto": df["efecto"].values, "Escasez desde": df["ruta_escasez_hoy"].values})


def html_hoja_ruta(hoja, fecha_hora, filtros):
    def bloque(df, titulo):
        filas = "".join(f"""<tr><td class="c">{i}</td><td class="c">{r['Ola']}</td>
            <td class="mono b">{escape(str(r['SKU']))}</td><td>{escape(str(r['Descripción']))}</td>
            <td class="c b org">{escape(str(r['Ubicación origen']))}</td><td class="c mono">{escape(str(r['LPN']))}</td>
            <td class="c b dst">{escape(str(r['Ubicación destino']))}</td><td class="c b qty">{int(r['Cajas'])}</td><td class="c small">{escape(str(r['Vence']))}</td>
            <td class="small">{escape(str(r['Efecto']))}</td>
            <td class="c small">{escape(str(r['Escasez desde']))}</td><td class="chk"></td></tr>"""
                        for i, (_, r) in enumerate(df.iterrows(), start=1))
        return f"""<div class="card"><table class="hd"><tr><td class="title">Reabasto ZM_CAJA · antes de liberar</td>
          <td class="r small"><b>Emisión:</b> {fecha_hora}</td></tr>
          <tr><td class="sub"><b>{titulo}</b> · Tareas: {len(df)} · Cajas: {int(df['Cajas'].sum())}</td>
          <td class="r small">{escape(filtros)}</td></tr></table>
          <table class="dt"><thead><tr><th>#</th><th>Ola</th><th>SKU</th><th>Descripción</th><th>Origen</th><th>LPN</th>
          <th>Destino</th><th>Cajas</th><th>Vence</th><th>Efecto</th><th>Escasez desde ruta</th><th>Check</th></tr></thead><tbody>{filas}</tbody></table>
          <table class="firmas"><tr><td>Operario: ________________</td><td>Hora inicio: ______</td>
          <td>Hora término: ______</td><td>Firma: ________________</td></tr></table></div>"""
    cuerpo = '<div class="salto"></div>'.join(bloque(g, f"Pasillo {p}") for p, g in hoja.groupby("Pasillo", sort=True))
    return f"""<style>
      body{{font-family:Arial,sans-serif}} .card{{border:2px solid #1E293B;border-radius:8px;padding:16px;margin-bottom:16px;color:#0F172A;background:#fff}}
      .hd{{width:100%;border-collapse:collapse}} .title{{font-size:19px;font-weight:bold;text-transform:uppercase}}
      .sub{{font-size:14px;color:#334155}} .small{{font-size:11px;color:#475569}} .r{{text-align:right}}
      .dt{{width:100%;border-collapse:collapse;margin-top:8px}} .dt th{{background:#1E293B;color:#fff;padding:6px 4px;font-size:11px;text-transform:uppercase}}
      .dt td{{border:1px solid #94A3B8;padding:6px 4px;font-size:12px}} .dt tr:nth-child(even) td{{background:#F8FAFC}}
      .c{{text-align:center}} .b{{font-weight:bold}} .mono{{font-family:monospace}} .org{{color:#1E3A8A}} .dst{{color:#065F46}}
      .qty{{font-size:15px}} .chk{{width:60px}} .firmas{{width:100%;margin-top:12px;font-size:12px}}
      @media print{{.no-print{{display:none!important}} .card{{border:none;padding:0}} .salto{{page-break-after:always}}
        .dt tr{{page-break-inside:avoid}} @page{{size:A4 landscape;margin:10mm}}}}</style>{cuerpo}"""


def _col(j):
    s, j = "", j + 1
    while j:
        j, r = divmod(j - 1, 26)
        s = chr(65 + r) + s
    return s


def a_excel(hojas, parametros):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter", datetime_format="dd-mm-yyyy") as xw:
        wb = xw.book
        head = wb.add_format({"bold": True, "font_name": "Arial", "font_size": 10, "bg_color": "#1F3864",
                              "font_color": "white", "border": 1, "valign": "vcenter", "text_wrap": True})
        body = wb.add_format({"font_name": "Arial", "font_size": 10})
        for nombre, df in hojas.items():
            df = df if df is not None and not df.empty else pd.DataFrame({"info": ["Sin registros"]})
            hoja = nombre[:31]
            df.to_excel(xw, sheet_name=hoja, index=False)
            ws = xw.sheets[hoja]
            for j, col in enumerate(df.columns):
                ws.write(0, j, col, head)
                ancho = max(len(str(col)), int(df[col].astype(str).str.len().max()))
                ws.set_column(j, j, min(max(ancho + 2, 8), 45), body)
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, len(df), len(df.columns) - 1)
            if "estado" in df.columns:
                j = df.columns.get_loc("estado")
                for txt, color in ESTADOS.items():
                    ws.conditional_format(1, 0, len(df), len(df.columns) - 1, {
                        "type": "formula", "criteria": f'=${_col(j)}2="{txt}"',
                        "format": wb.add_format({"bg_color": color})})
        pd.DataFrame(list(parametros.items()), columns=["Parámetro", "Valor"]).to_excel(
            xw, sheet_name="Parámetros", index=False)
        xw.sheets["Parámetros"].set_column(0, 1, 45, body)
    return buf.getvalue()


def a_excel_hoja(hoja, titulo):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        hoja.to_excel(xw, sheet_name="Hoja operarios", index=False, startrow=2)
        ws, wb = xw.sheets["Hoja operarios"], xw.book
        ws.write(0, 0, titulo, wb.add_format({"bold": True, "font_size": 13}))
        head = wb.add_format({"bold": True, "bg_color": "#1E293B", "font_color": "white", "border": 1})
        for j, c in enumerate(list(hoja.columns) + ["Check (✓)"]):
            ws.write(2, j, c, head)
            ws.set_column(j, j, 14 if c not in ("Descripción",) else 38)
        ws.set_landscape(); ws.set_paper(9); ws.fit_to_pages(1, 0); ws.repeat_rows(2)
    return buf.getvalue()
