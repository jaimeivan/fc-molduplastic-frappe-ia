-- Índices analíticos del asistente IA sobre tabPieza.
--
-- `bench migrate` NO los crea: viven fuera de los DocTypes (tabPieza es de la
-- app hermana). Sin ellos el asistente no funciona: `query_executor._indice_forzado`
-- hace `FORCE INDEX` con estos nombres y MySQL/MariaDB responde
--   ERROR 1176: Key 'idx_ia_an_ing' doesn't exist in table 'tabPieza'
-- (el chat muestra "Error de conexión (HTTP 500)").
--
-- Uso (MariaDB 10.11, el motor de este bench):
--   bench --site <site> mariadb < scripts/indices_ia.sql
--
-- - Es re-ejecutable: solo crea el índice que falta (consulta information_schema).
-- - Crea en línea (ALGORITHM=INPLACE, LOCK=NONE), sin bloquear escrituras.
-- - En 3.3 M filas tarda del orden de 30-60 min y deja ~3.3 GB de índices:
--   ejecútalo en ventana de mantenimiento y con disco libre.
-- - Pide CREATE ROUTINE (el usuario de BD de Frappe lo tiene). Si usas una
--   cuenta restringida, al final del archivo está la lista equivalente de
--   ALTER TABLE para correr a mano.

DROP PROCEDURE IF EXISTS _ia_crear_indice;

DELIMITER //
CREATE PROCEDURE _ia_crear_indice(IN p_indice VARCHAR(64), IN p_columnas TEXT)
BEGIN
	SET @existe = (SELECT COUNT(*) FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabPieza'
		AND INDEX_NAME = p_indice);
	IF @existe = 0 THEN
		SET @ddl = CONCAT('ALTER TABLE `tabPieza` ADD INDEX `', p_indice, '` (', p_columnas,
			'), ALGORITHM=INPLACE, LOCK=NONE');
		PREPARE stmt FROM @ddl;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
	END IF;
END //
DELIMITER ;

-- Listados por tenant (Taller y Recoleccion en el Desk).
CALL _ia_crear_indice('idx_ia_aseg_taller', 'aseguradora, taller_asignado');
CALL _ia_crear_indice('idx_ia_aseg_recoleccion', 'aseguradora, id_recoleccion');

-- Base ingreso: efectividad, resumen, precios, por taller / parte / modelo.
CALL _ia_crear_indice('idx_ia_an_ing',
	'aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, fecha_hora_recoleccion, taller_asignado, parte_pieza, modelo, preciovale');

-- Base recolección: rezagadas y la pata B de "recolectadas".
CALL _ia_crear_indice('idx_ia_an_rec',
	'aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, rutas_validas, id_estatus, taller_asignado, parte_pieza, preciovale');

-- Marca / región (las dos bases).
CALL _ia_crear_indice('idx_ia_an_marca',
	'aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, marca, estadolocalidad, fecha_hora_recoleccion, preciovale');
CALL _ia_crear_indice('idx_ia_an_rec_marca',
	'aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, rutas_validas, id_estatus, marca, estadolocalidad, preciovale');

-- Siniestros únicos por período.
CALL _ia_crear_indice('idx_ia_aseg_carga_siniestro', 'aseguradora, fecha_hora_carga, siniestro');

-- Barra "Información del ... al ..." del chat (MIN(creation) / MAX(modified)).
CALL _ia_crear_indice('idx_ia_aseg_creation', 'aseguradora, creation');
CALL _ia_crear_indice('idx_ia_aseg_modified', 'aseguradora, modified');
CALL _ia_crear_indice('idx_ia_creation', 'creation');

-- Catálogos del asistente (DISTINCT de parte_pieza y de tipo/submarca).
CALL _ia_crear_indice('idx_ia_parte', 'parte_pieza');
CALL _ia_crear_indice('idx_ia_tipo', 'tipo');

DROP PROCEDURE _ia_crear_indice;

-- Debe devolver 12 filas en "OK". Si alguna dice FALTA, revisa el error de esa
-- sentencia antes de abrir el chat.
SELECT esperado.nombre AS indice, IF(existente.idx IS NULL, 'FALTA', 'OK') AS estado
FROM (
	SELECT 'idx_ia_aseg_taller' AS nombre
	UNION ALL SELECT 'idx_ia_aseg_recoleccion'
	UNION ALL SELECT 'idx_ia_an_ing'
	UNION ALL SELECT 'idx_ia_an_rec'
	UNION ALL SELECT 'idx_ia_an_marca'
	UNION ALL SELECT 'idx_ia_an_rec_marca'
	UNION ALL SELECT 'idx_ia_aseg_carga_siniestro'
	UNION ALL SELECT 'idx_ia_aseg_creation'
	UNION ALL SELECT 'idx_ia_aseg_modified'
	UNION ALL SELECT 'idx_ia_creation'
	UNION ALL SELECT 'idx_ia_parte'
	UNION ALL SELECT 'idx_ia_tipo'
) esperado
LEFT JOIN (
	SELECT DISTINCT INDEX_NAME AS idx
	FROM information_schema.STATISTICS
	WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabPieza'
) existente ON existente.idx = esperado.nombre
ORDER BY esperado.nombre;

-- ---------------------------------------------------------------------------
-- Variante sin procedimiento (solo si la cuenta no puede crear procedimientos).
-- Ejecuta únicamente las líneas cuyo índice no exista; cada una falla con 1061
-- si el índice ya está.
-- ---------------------------------------------------------------------------
-- ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_taller (aseguradora, taller_asignado), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_recoleccion (aseguradora, id_recoleccion), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_an_ing (aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, fecha_hora_recoleccion, taller_asignado, parte_pieza, modelo, preciovale), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_an_rec (aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, rutas_validas, id_estatus, taller_asignado, parte_pieza, preciovale), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_an_marca (aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, marca, estadolocalidad, fecha_hora_recoleccion, preciovale), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_an_rec_marca (aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, rutas_validas, id_estatus, marca, estadolocalidad, preciovale), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_carga_siniestro (aseguradora, fecha_hora_carga, siniestro), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_creation (aseguradora, creation), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_modified (aseguradora, modified), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_creation (creation), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_parte (parte_pieza), ALGORITHM=INPLACE, LOCK=NONE;
-- ALTER TABLE tabPieza ADD INDEX idx_ia_tipo (tipo), ALGORITHM=INPLACE, LOCK=NONE;
