# Escasez proyectada ZM_CAJA – CD Coquimbo

App Streamlit que, **antes de liberar las rutas**, cruza toda la venta del fleetloader de Truck
contra el stock de las posiciones de picking **ZM_CAJ** del WMS. Así se repone de una vez lo que
va a faltar en vez de ir eliminando la escasez ruta por ruta.

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

Cuando cambien preferencias, capacidades o zonas en el WMS, reemplaza el CSV respectivo (mismas columnas).

## Cálculo
1. SKU TRUCK → SAP (se ignoran los ceros a la izquierda: `00450604` = `450604`). Se suma el mismo SKU repetido en varias bahías.
2. Tabla dinámica: venta total por SKU SAP y matriz SKU × ruta.
3. Por SKU con posición ZM_CAJ: **necesidad = venta − stock picking (estado D)**.
   - **Ola 1** = lo que cabe ahora (capacidad − stock) → se hace antes de liberar.
   - **Llenados extra** = reposiciones durante el picking cuando la posición es más chica que la venta.
4. **Escasez desde ruta**: primera ruta, por `LOAD_PRIORITY`, que deja la posición en cero (con stock de hoy y con la posición llena).
5. **Quiebre CD**: ALMAC/ALMPIC no alcanza para cubrir la necesidad.
6. Movimientos desde ALMAC/ALMPIC: estado D, sin vencidos, FEFO y restos de pallet primero.

Los SKU sin posición ZM_CAJA (ZM_PICK / ZM_PALLET) quedan fuera: van por trabajo dirigido del WMS.

## Opciones
- **Rutas ya liberadas (excluir)**: para recalcular a mitad de jornada.
- **Descontar pallets completos**: las cajas que salen como pallet completo no se cuentan contra ZM_CAJA.
- **Holgura (%)** sobre la venta y **tolerancia FEFO (días)**.

## Ejecutar
```
pip install -r requirements.txt
streamlit run app.py
```
Para publicarla: share.streamlit.io → este repositorio → `app.py`.
