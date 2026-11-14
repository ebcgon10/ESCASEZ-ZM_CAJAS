# Escasez proyectada ZM_CAJA – CD Coquimbo

App Streamlit que, **antes de liberar las rutas**, cruza toda la venta del fleetloader de Truck
contra el stock de las posiciones de picking **ZM_CAJ** del WMS. Así se repone de una vez lo que
va a faltar en vez de ir eliminando la escasez ruta por ruta.

## Uso
1. Sube el **fleetloader** (`FLETBxxxx.TXT`) y las **operaciones de vida útil** (`.csv`).
2. Presiona **Procesar**.
3. Sale una sola hoja, ordenada por pasillo: **SKU · Descripción · Origen (almacenamiento) · Destino (surtido) ·
   Cantidad · Check**. Se imprime o se descarga en Excel.

**Cantidad** = venta del fleetloader − stock en la posición ZM_CAJA: lo que hay que reponer para cumplir todo lo
pedido. Si se toma de varios LPN, sale una fila por origen. Origen `SIN STOCK` = no hay en almacenamiento.

## Archivos que se suben
| Archivo | Origen | Uso |
|---|---|---|
| `FLETBxxxx.TXT` (fleetloader) | Truck, separado por `\|` | Cajas por ruta (`ROUTE_ID`), orden de carga (`LOAD_PRIORITY`), `SKU`, `QUANTITY` |
| `OPERACIONES_DE_VIDA_UTIL.csv` | WMS | Stock por ubicación / LPN / vencimiento |

Se puede subir más de un fleetloader si la venta del día viene en varias sesiones.

## Maestros incluidos (`maestros/`)
- `bbdd_codigos.csv`: SKU TRUCK ↔ SAP. En Streamlit Cloud se lee primero la versión vigente del repo
  [sistema-revision-venta](https://github.com/ebcgon10/sistema-revision-venta); esta copia es el respaldo.
- `ubicaciones.csv`: capacidad máxima por ubicación.
- `preferencia.csv`: artículo asignado y secuencia de ruta.
- `zonas.csv`: zona de trabajo / movimiento (`ZM_CAJ`, `ZM_PICK`, …).
- `paletizado.csv`: cajas por pallet (si falta, se usa la norma del Truck: `QTY_PER_LAYER × LAYERS_PER_PALLET`).

Maestros cargados desde *Datos_para_armar.xlsx* (14-11-2026). Para actualizarlos, reemplaza
`ubicaciones.csv`, `preferencia.csv` y `zonas.csv` en `maestros/` (mismas columnas).

## Cálculo
1. SKU TRUCK → SAP (se ignoran los ceros a la izquierda: `00450604` = `450604`). Se suma el mismo SKU repetido en varias bahías.
2. Tabla dinámica: venta total por SKU SAP y matriz SKU × ruta.
3. Por SKU con posición ZM_CAJ: **necesidad = venta − stock picking (estado D)**.
   - **Ola 1** = lo que cabe ahora (capacidad − stock) → se hace antes de liberar.
   - **Llenados extra** = reposiciones durante el picking cuando la posición es más chica que la venta.
4. **Escasez desde ruta**: primera ruta, por `LOAD_PRIORITY`, que deja la posición en cero (con stock de hoy y con la posición llena).
5. **Quiebre CD**: los orígenes válidos no alcanzan para cubrir la necesidad.
6. Movimientos: estado D, sin vencidos, **FEFO 100 %** (sin tolerancia). Origen: **ALMAC, ALMPIC, MIX-01,
   REBA-01 y STREC01..n**. No se usan reempaque, subestándar, revisión, consumo, merma ni retenido.
   (Opcional: la posición ZM_PICK del mismo SKU.)
   - A igual vencimiento: **restos en 2°/3° nivel** (menos cajas primero) → restos en piso →
     pallets completos en piso → pallets completos en altura.
   - Si el resto en altura cabe entero en el espacio libre de la posición, se baja completo.
   - **Bajada de restos (R):** aunque el SKU ya esté cubierto, si el siguiente lote FEFO es un resto en altura
     que cabe entero en la posición, se propone bajarlo para liberar el nivel.
   - Control FEFO automático: la app verifica que ningún LPN más antiguo quede sin consumir.
   - Alerta cuando la posición de picking tiene un lote más nuevo que el almacenamiento.

Los SKU sin posición ZM_CAJA (ZM_PICK / ZM_PALLET) quedan fuera: van por trabajo dirigido del WMS.


## Ejecutar
```
pip install -r requirements.txt
streamlit run app.py
```
Para publicarla: share.streamlit.io → este repositorio → `app.py`.
