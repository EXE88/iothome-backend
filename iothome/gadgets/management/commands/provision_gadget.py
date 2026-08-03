"""Hand a gadget to a user without going through checkout — for development.

    python manage.py provision_gadget --email you@gmail.com --type smart-lamp

Prints the secret key, which is the one thing the firmware needs.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from gadgets.models import Gadget, GadgetType
from purchases.models import Product

User = get_user_model()


class Command(BaseCommand):
    help = "Create a gadget for a user directly (development helper)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--type", required=True, help="GadgetType slug")
        parser.add_argument("--name", default="")
        parser.add_argument("--ssid", default="dev-wifi")
        parser.add_argument("--wifi-password", default="dev-password")

    def handle(self, *args, **options):
        user = User.objects.filter(email=options["email"]).first()
        if user is None:
            raise CommandError(f"No user with email {options['email']}.")
        gadget_type = GadgetType.objects.filter(slug=options["type"]).first()
        if gadget_type is None:
            raise CommandError(
                f"No gadget type '{options['type']}'. Run seed_catalog first."
            )
        product = gadget_type.products.first()
        if product is None:
            raise CommandError(f"No product sells '{options['type']}'.")

        gadget = Gadget.objects.create(
            owner=user,
            product=product,
            gadget_type=gadget_type,
            name=options["name"],
            wifi_ssid=options["ssid"],
            wifi_password=options["wifi_password"],
            status=Gadget.STATUS_ASSEMBLED,
        )
        self.stdout.write(self.style.SUCCESS("Gadget created."))
        self.stdout.write(f"  uid:        {gadget.uid}")
        self.stdout.write(f"  secret_key: {gadget.secret_key}")
        self.stdout.write(f"  ws path:    /ws/device/{gadget.uid}/")
