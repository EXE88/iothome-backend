import logging
from urllib.parse import urlencode, urlparse, urlunparse

from django.conf import settings
from django.shortcuts import redirect
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEmailVerified

from .models import Order, Product
from .serializers import OrderCreateSerializer, OrderSerializer, ProductSerializer
from .services import CheckoutError, create_order, finalize_payment, start_payment

logger = logging.getLogger("iothome.purchases")


class ProductListView(generics.ListAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    queryset = Product.objects.select_related("gadget_type").filter(is_active=True)


class ProductDetailView(generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"
    queryset = Product.objects.select_related("gadget_type").filter(is_active=True)


def allowed_return_origin(request):
    """The origin this request came from, if we serve that origin.

    Checked against CORS_ALLOWED_ORIGINS rather than trusted, because the
    result becomes a redirect target: an unchecked `Origin` header would turn
    the payment callback into an open redirect that a phishing page could
    point anywhere. Anything unrecognised falls back to the configured
    default.
    """
    origin = request.headers.get("Origin", "")
    return origin if origin in settings.CORS_ALLOWED_ORIGINS else ""


def payment_result_url(order):
    """Where to send the browser once the gateway has been settled.

    The path is configured; the host follows the buyer. The site answers on
    more than one hostname in development and those hostnames do not share
    cookies, so returning someone to a different one signs them out — or
    worse, signs them in as whoever last used that hostname.
    """
    configured = urlparse(settings.FRONTEND_PAYMENT_RESULT_URL)
    if not order.return_origin:
        return settings.FRONTEND_PAYMENT_RESULT_URL
    chosen = urlparse(order.return_origin)
    return urlunparse(
        (chosen.scheme, chosen.netloc, configured.path, "", "", "")
    )


def orders_of(user):
    """One query shape for both order views, with the lines pulled in."""
    return (
        Order.objects.prefetch_related(
            "items__product__gadget_type", "payments"
        ).filter(user=user)
    )


class OrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get_queryset(self):
        return orders_of(self.request.user)


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]
    lookup_field = "uid"

    def get_queryset(self):
        return orders_of(self.request.user)


class CheckoutView(APIView):
    """Create the order and hand the browser a gateway URL to redirect to."""

    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = create_order(
                user=request.user,
                items=data["items"],
                wifi_ssid=data["wifi_ssid"],
                wifi_password=data["wifi_password"],
                receiver_name=data["receiver_name"],
                receiver_phone=data["receiver_phone"],
                shipping_address=data["shipping_address"],
                postal_code=data.get("postal_code", ""),
                return_origin=allowed_return_origin(request),
            )
            payment, payment_url = start_payment(
                order, settings.ZARINPAL_CALLBACK_URL
            )
        except CheckoutError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {
                "order": OrderSerializer(order).data,
                "payment_url": payment_url,
                "authority": payment.authority,
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentVerifyView(APIView):
    """Callback target for Zarinpal.

    The gateway sends the *browser* here, not an API client, so the response
    is a redirect back to the frontend with the outcome in the query string.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        authority = request.query_params.get("Authority", "")
        gateway_status = request.query_params.get("Status", "")

        result = {"authority": authority}
        destination = settings.FRONTEND_PAYMENT_RESULT_URL
        try:
            payment, gadgets = finalize_payment(
                authority=authority, gateway_status=gateway_status
            )
            result["status"] = payment.status
            result["order"] = str(payment.order.uid)
            result["ref_id"] = payment.ref_id
            result["gadgets"] = len(gadgets)
            destination = payment_result_url(payment.order)
        except CheckoutError as exc:
            logger.warning("payment callback failed for %s: %s", authority, exc)
            result["status"] = "failed"
            result["detail"] = str(exc)

        return redirect(f"{destination}?{urlencode(result)}")
