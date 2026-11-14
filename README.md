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

Maestros cargados desde *Datos_para_armar.xlsx* (14-11-2026). Para actualizarlos:
- **En la app:** barra lateral → *Maestros WMS (opcional)* → sube el Excel con hojas UBICACIONES / PREFERENCIA / ZM
  (reemplaza los del repo solo en esa sesión).
- **En el repo:** reemplaza `ubicaciones.csv`, `preferencia.csv` y `zonas.csv` (mismas columnas).

La pestaña **ADC maestro WMS** arma el control de cambios sugerido (capacidades menores al stock, posiciones con
otro artículo, ubicaciones con 2 artículos) para validarlo antes de cargarlo en el WMS.

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

## Opciones
- **Rutas ya liberadas (excluir)**: para recalcular a mitad de jornada.
- **Descontar pallets completos**: las cajas que salen como pallet completo no se cuentan contra ZM_CAJA.
- **Holgura (%)** sobre la venta.
- **Bajar restos en altura** y **usar la ZM_PICK del mismo SKU** como origen.

## Ejecutar
```
pip install -r requirements.txt
streamlit run app.py
```
Para publicarla: share.streamlit.io → este repositorio → `app.py`.
