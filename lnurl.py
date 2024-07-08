import base64
from http import HTTPStatus

from fastapi import HTTPException, Query, Request

from lnbits import bolt11
from lnbits.core.services import create_invoice
from lnbits.core.views.api import pay_invoice
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis
from loguru import logger

from . import lnurldevice_ext
from .crud import (
    create_lnurldevicepayment,
    get_lnurldevice,
    get_lnurldevicepayment,
    update_lnurldevicepayment,
)
from fastapi.responses import JSONResponse
from .helpers import register_atm_payment, xor_decrypt

@lnurldevice_ext.get(
    "/api/v1/lnurl/{device_id}",
    status_code=HTTPStatus.OK,
    name="lnurldevice.lnurl_v1_params",
)
async def lnurl_v1_params(
    request: Request,
    device_id: str,
    p: str = Query(None),
    atm: str = Query(None),
    gpio: str = Query(None),
    profit: str = Query(None),
    amount: str = Query(None),
):
    return await lnurl_params(request, device_id, p, atm, gpio, profit, amount)


@lnurldevice_ext.get(
    "/api/v2/lnurl/{device_id}",
    status_code=HTTPStatus.OK,
    name="lnurldevice.lnurl_v2_params",
)
async def lnurl_v2_params(
    request: Request,
    device_id: str,
    p: str = Query(None),
    atm: str = Query(None),
    pin: str = Query(None),
    amount: str = Query(None),
    duration: str = Query(None),
    variable: bool = Query(None),
    comment: bool = Query(None),
):
    return await lnurl_params(
        request, device_id, p, atm, pin, amount, duration, variable, comment
    )


async def lnurl_params(
    request: Request,
    device_id: str,
    p: str,
    atm: str,
    pin: str,
    amount: str,
    duration: str,
    variable: bool = Query(None),
    comment: bool = Query(None),
):
    device = await get_lnurldevice(device_id, request)
    if not device:
        return {
            "status": "ERROR",
            "reason": f"lnurldevice {device_id} not found on this server",
        }

    if device.device == "switch":
        price_msat = int(
            (
                await fiat_amount_as_satoshis(float(amount), device.currency)
                if device.currency != "sat"
                else float(amount)
            )
            * 1000
        )

        # Check they're not trying to trick the switch!
        check = False
        if device.extra and "atm" not in device.extra:
            for extra in device.extra:
                if (
                    extra.pin == int(pin)
                    and extra.duration == int(duration)
                    and bool(extra.variable) == bool(variable)
                    and bool(extra.comment) == bool(comment)
                ):
                    check = True
                    continue
        if not check:
            return {"status": "ERROR", "reason": "Extra params wrong"}

        lnurldevicepayment = await create_lnurldevicepayment(
            deviceid=device.id,
            payload=duration,
            sats=price_msat,
            pin=pin,
            payhash="bla",
        )
        if not lnurldevicepayment:
            return {"status": "ERROR", "reason": "Could not create payment."}
        resp = {
            "tag": "payRequest",
            "callback": str(
                request.url_for(
                    "lnurldevice.lnurl_callback",
                    paymentid=lnurldevicepayment.id,
                    variable=variable,
                )
            ),
            "minSendable": price_msat,
            "maxSendable": price_msat,
            "metadata": device.lnurlpay_metadata,
        }
        if comment == True:
            resp["commentAllowed"] = 1500
        if variable == True:
            resp["maxSendable"] = price_msat * 360
        return resp

    if len(p) % 4 > 0:
        p += "=" * (4 - (len(p) % 4))

    data = base64.urlsafe_b64decode(p)
    try:
        pin, amount_in_cent = xor_decrypt(device.key.encode(), data)
    except Exception as exc:
        return {"status": "ERROR", "reason": str(exc)}

    price_msat = (
        await fiat_amount_as_satoshis(float(amount_in_cent) / 100, device.currency)
        if device.currency != "sat"
        else amount_in_cent
    ) * 1000

    if atm:
        lnurldevicepayment, price_msat = await register_atm_payment(device, p)
        if lnurldevicepayment["status"] == "ERROR":
            return lnurldevicepayment
        if not lnurldevicepayment:
            return {"status": "ERROR", "reason": "Could not create ATM payment."}
        return {
            "tag": "withdrawRequest",
            "callback": str(request.url_for(
                "lnurldevice.lnurl_callback", paymentid=lnurldevicepayment.id, variable=None
            )),
            "k1": p,
            "minWithdrawable": price_msat * 1000,
            "maxWithdrawable": price_msat * 1000,
            "defaultDescription": f"{device.title} - pin: {lnurldevicepayment.pin}",
        }
    price_msat = int(price_msat * ((device.profit / 100) + 1) / 1000)

    lnurldevicepayment = await create_lnurldevicepayment(
        deviceid=device.id,
        payload=p,
        sats=price_msat * 1000,
        pin=pin,
        payhash="payment_hash",
    )
    if not lnurldevicepayment:
        return {"status": "ERROR", "reason": "Could not create payment."}
    return {
        "tag": "payRequest",
        "callback": str(request.url_for(
            "lnurldevice.lnurl_callback", paymentid=lnurldevicepayment.id, variable=None
        )),
        "minSendable": price_msat * 1000,
        "maxSendable": price_msat * 1000,
        "metadata": device.lnurlpay_metadata,
    }


@lnurldevice_ext.get(
    "/api/v1/lnurl/cb/{paymentid}",
    status_code=HTTPStatus.OK,
    name="lnurldevice.lnurl_callback",
)
async def lnurl_callback(
    request: Request,
    paymentid: str,
    variable: str = Query(None),
    amount: int = Query(None),
    comment: str = Query(None),
    pr: str = Query(None),
    k1: str = Query(None),
):
    lnurldevicepayment = await get_lnurldevicepayment(paymentid)
    if not lnurldevicepayment:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="lnurldevicepayment not found."
        )
    device = await get_lnurldevice(lnurldevicepayment.deviceid, request)
    if not device:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="lnurldevice not found."
        )
    if device.device == "atm":
        if lnurldevicepayment.payload == lnurldevicepayment.payhash:
            return {"status": "ERROR", "reason": "Payment already claimed"}
        if not pr:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN, detail="No payment request"
            )
        invoice = bolt11.decode(pr)
        if not invoice.payment_hash:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN, detail="Not valid payment request"
            )
        else:
            if lnurldevicepayment.payload != k1:
                return {"status": "ERROR", "reason": "Bad K1"}
            if lnurldevicepayment.payhash != "payment_hash":
                return {"status": "ERROR", "reason": "Payment already claimed"}
            try:
                lnurldevicepayment_updated = await update_lnurldevicepayment(
                    lnurldevicepayment_id=paymentid, payhash=lnurldevicepayment.payload
                )
                assert lnurldevicepayment_updated
                await pay_invoice(
                    wallet_id=device.wallet,
                    payment_request=pr,
                    max_sat=int(lnurldevicepayment_updated.sats / 1000),
                    extra={"tag": "withdraw"},
                )
            except Exception:
                return {
                    "status": "ERROR",
                    "reason": "Payment failed, use a different wallet.",
                }
            return {"status": "OK"}
    if device.device == "switch":
        if not amount:
            return {"status": "ERROR", "reason": "No amount"}

        payment_hash, payment_request = await create_invoice(
            wallet_id=device.wallet,
            amount=int(amount / 1000),
            memo=f"{device.id} pin {lnurldevicepayment.pin} ({lnurldevicepayment.payload} ms)",
            unhashed_description=device.lnurlpay_metadata.encode(),
            extra={
                "tag": "Switch",
                "pin": str(lnurldevicepayment.pin),
                "amount": str(int(amount)),
                "comment": comment,
                "variable": variable,
                "id": paymentid,
            },
        )

        lnurldevicepayment = await update_lnurldevicepayment(
            lnurldevicepayment_id=paymentid, payhash=payment_hash
        )
        resp = {
            "pr": payment_request,
            "successAction": {
                "tag": "message",
                "message": f"{int(amount / 1000)}sats sent",
            },
            "routes": [],
        }

        return resp

    payment_hash, payment_request = await create_invoice(
        wallet_id=device.wallet,
        amount=int(lnurldevicepayment.sats / 1000),
        memo=device.title,
        unhashed_description=device.lnurlpay_metadata.encode(),
        extra={"tag": "PoS"},
    )
    lnurldevicepayment = await update_lnurldevicepayment(
        lnurldevicepayment_id=paymentid, payhash=payment_hash
    )

    return {
        "pr": payment_request,
        "successAction": {
            "tag": "url",
            "description": "Check the attached link",
            "url": str(request.url_for("lnurldevice.displaypin", paymentid=paymentid)),
        },
        "routes": [],
    }
