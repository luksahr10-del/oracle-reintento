"""Reintenta crear una VM Always Free de Oracle (A1.Flex) hasta que haya capacidad, en GitHub Actions.

Todo lo de la cuenta llega por variables de entorno (secretos del repositorio). Cada ejecución dura ~5 h 50 min
y el flujo se relanza solo. Al conseguir la VM avisa por Telegram y apaga el flujo.
"""
import datetime
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

import oci

NOMBRE = "viernes"
FORMAS = [(2, 12), (1, 6)]  # se prueba la grande y, si no hay, la chica
ESPERA = 180  # segundos entre rondas (más un poco al azar)
DURACION = 5 * 3600 + 50 * 60  # GitHub corta los trabajos a las 6 h

config = {
    "user": os.environ["OCI_USER"],
    "tenancy": os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "key_file": os.environ["OCI_KEY_FILE"],
    "region": os.environ.get("OCI_REGION", "sa-santiago-1"),
}
TENANCY = config["tenancy"]
SUBNET = os.environ["OCI_SUBNET"]
compute = oci.core.ComputeClient(config)
red = oci.core.VirtualNetworkClient(config)
identidad = oci.identity.IdentityClient(config)


def log(texto: str) -> None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {texto}", flush=True)


_chequeo = {"id": None}


def publicar(rondas: int, ultimo: str, terminado: bool = False) -> None:
    """Deja el avance en un "check run" público ("estado") que se lee en vivo con la API de GitHub sin llave:
    título "<rondas> rondas", resumen con la hora y la última respuesta de Oracle."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return
    datos = {"output": {"title": f"{rondas} rondas",
                        "summary": f"{datetime.datetime.now(datetime.UTC):%Y-%m-%dT%H:%M:%SZ} {ultimo}"}}
    if terminado:
        datos |= {"status": "completed", "conclusion": "success"}
    if _chequeo["id"]:
        url, metodo = f"https://api.github.com/repos/{repo}/check-runs/{_chequeo['id']}", "PATCH"
    else:
        url, metodo = f"https://api.github.com/repos/{repo}/check-runs", "POST"
        datos |= {"name": "estado", "head_sha": os.environ["GITHUB_SHA"], "status": "in_progress"}
    peticion = urllib.request.Request(url, json.dumps(datos).encode(), method=metodo, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(peticion, timeout=20) as r:
            _chequeo["id"] = json.load(r)["id"]
    except Exception as e:
        log(f"No pude publicar el avance: {type(e).__name__}")


def avisar(texto: str) -> None:
    datos = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT"], "text": texto}).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    for _ in range(5):
        try:
            with urllib.request.urlopen(url, datos, timeout=30) as r:
                if json.load(r).get("ok"):
                    return
        except Exception as e:
            log(f"No pude avisar por Telegram: {type(e).__name__}")
        time.sleep(20)


def imagen() -> str:
    imgs = compute.list_images(TENANCY, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                               shape="VM.Standard.A1.Flex", sort_by="TIMECREATED", sort_order="DESC").data
    return imgs[0].id


def vm_existente():
    for i in compute.list_instances(compartment_id=TENANCY, display_name=NOMBRE).data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return i
    return None


def ip_publica(instancia) -> str | None:
    for _ in range(40):
        for adj in compute.list_vnic_attachments(TENANCY, instance_id=instancia.id).data:
            if adj.lifecycle_state == "ATTACHED":
                ip = red.get_vnic(adj.vnic_id).data.public_ip
                if ip:
                    return ip
        time.sleep(15)
    return None


def lanzar(ad: str, ocpus: int, memoria: int, id_imagen: str):
    detalles = oci.core.models.LaunchInstanceDetails(
        compartment_id=TENANCY, availability_domain=ad, display_name=NOMBRE,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memoria),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=id_imagen, boot_volume_size_in_gbs=50),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=SUBNET, assign_public_ip=True),
        metadata={"ssh_authorized_keys": os.environ["SSH_PUB"]})
    return compute.launch_instance(detalles).data


def terminar(instancia, ocpus: int, memoria: int) -> None:
    ip = ip_publica(instancia)
    log(f"VM creada ({ocpus} OCPU / {memoria} GB)")  # la IP no va al log público
    avisar(f"Lukas, conseguí el servidor de Oracle: {ocpus} procesadores y {memoria} gigas, "
           f"IP {ip or 'pendiente'}. Abre Claude Code para configurarlo.")


def main() -> int:
    """0: sigue mañana (otra ejecución); 3: listo o detenido (apagar el flujo)."""
    inicio = time.monotonic()
    log("Empiezo a reintentar")
    if vm_existente():
        log("La VM ya existe; no hay nada que hacer")
        publicar(0, "la VM ya existe", terminado=True)
        return 3
    ad = identidad.list_availability_domains(TENANCY).data[0].name
    ronda = 0
    while time.monotonic() - inicio < DURACION:
        ronda += 1
        try:
            id_imagen = imagen()
            for ocpus, memoria in FORMAS:
                try:
                    instancia = lanzar(ad, ocpus, memoria, id_imagen)
                except oci.exceptions.ServiceError as e:
                    if "capacity" in e.message.lower():
                        continue
                    if e.status == 429:
                        log("Oracle pide calma (429); espero más")
                        publicar(ronda, "Oracle pide calma (429)")
                        time.sleep(300)
                        break
                    if e.code == "LimitExceeded":
                        log("Límite de la cuenta excedido")
                        publicar(ronda, "detenido: límite de la cuenta excedido", terminado=True)
                        avisar("Lukas, el reintento de la VM de Oracle (GitHub) se detuvo: Oracle dice que se "
                               "excede el límite gratuito. Abre Claude Code para revisarlo.")
                        return 3
                    raise
                publicar(ronda, f"VM creada ({ocpus} OCPU / {memoria} GB)", terminado=True)
                terminar(instancia, ocpus, memoria)
                return 3
            else:
                if ronda % 20 == 1:
                    log(f"Ronda {ronda}: sin capacidad todavía")
                publicar(ronda, "sin capacidad")
        except Exception as e:
            log(f"Error en la ronda {ronda}: {type(e).__name__}: {getattr(e, 'status', '')} {getattr(e, 'code', '')}")
            publicar(ronda, f"error {type(e).__name__} {getattr(e, 'status', '')}".strip())
        time.sleep(ESPERA + random.randint(0, 60))
    log(f"Fin de esta tanda ({ronda} rondas); sigue la próxima")
    publicar(ronda, "fin de la tanda; sigue la próxima", terminado=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
