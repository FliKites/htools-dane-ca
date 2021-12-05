import datetime
import uuid
import tempfile
import hashlib
import requests
from subprocess import Popen, PIPE, DEVNULL

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

TIME_DELTA = datetime.timedelta(days=5)


class DaneBackend(object):
    def __init__(self, config):
        self.config = config

    def sign(self, csr, subjectDN, subjectAltNames, email):
        print('csr:', csr)
        print('subjectDN:', subjectDN)
        print('subjectAltNames:', subjectAltNames)
        print('email:', email)

        # Load CSR
        csr_obj = x509.load_der_x509_csr(csr)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, subjectDN),
        ])

        # Generate temporary CA
        ca_cert, ca_privkey = self.generate_ephemeral_ca()

        # Create certificate
        certificate = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            csr_obj.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow() - TIME_DELTA
        ).not_valid_after(
            datetime.datetime.utcnow() + TIME_DELTA
        ).add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in subjectAltNames]),
            critical=False,
            # Sign our certificate with our private key
        )

        # Sign certificate with CA's key
        certificate = certificate.sign(ca_privkey, hashes.SHA256())

        # Bundle domain and CA certificate into  fullchain (PKCS#7, DER)
        bundle = self.create_fullchain([
            ca_cert.public_bytes(Encoding.PEM),
            certificate.public_bytes(Encoding.PEM)
        ])
        print("bundle:", bundle)

        # TLSA
        cert_bytes = certificate.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        tlsa_digest = hashlib.sha256(cert_bytes).hexdigest()
        print('TLSA:', f'_443._tcp.{subjectDN}. TLSA 3 1 1 {tlsa_digest}')

        # Send email
        try:
            self.send_cert_issue_email(email, subjectDN, tlsa_digest)
        except Exception as e:
            print(e)

        return (bundle, None)

    def generate_ephemeral_ca(self):

        # Generate CA's key pair
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        public_key = private_key.public_key()

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME,
                               u'Handshake Tools Ephemeral CA'),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME,
                               u'Handshake Tools'),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,
                               u'ACME'),
        ])

        # Create CA certificate
        builder = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).not_valid_before(
            datetime.datetime.utcnow() - TIME_DELTA
        ).not_valid_after(
            datetime.datetime.utcnow() + TIME_DELTA
        ).serial_number(
            int(uuid.uuid4())
        ).public_key(
            public_key
        ).add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True,
        )

        # Sign CA certificate with CA key
        certificate = builder.sign(
            private_key=private_key, algorithm=hashes.SHA256(),
        )

        return certificate, private_key

    def create_fullchain(self, certs):
        # OpenSSL only reads certificates only from files and not stdin,
        # so we write them to NamedTemporaryFiles which are deleted on close
        files = []
        certfile_args = []
        for cert in certs:
            f = tempfile.NamedTemporaryFile()
            f.write(cert)
            f.flush()
            certfile_args += ["-certfile", f.name]
            files.append(f)

        print("certfile_args:", certfile_args)

        proc = Popen(
            ["openssl", "crl2pkcs7", "-nocrl", "-outform", "DER"] + certfile_args,
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
        )
        pem_cert = proc.stdout.read()
        print("combined fullchain:", pem_cert)

        for file in files:
            file.close()

        return pem_cert

    def send_cert_issue_email(self, email, domain, digest):
        print(f'Bearer {self.config["sendgrid"]["api_key"]}')
        headers = {
            'Authorization': f'Bearer {self.config["sendgrid"]["api_key"]}',
            'Content-Type': 'application/json'
        }

        payload = {
            "template_id": self.config["sendgrid"]["template_id"],
            "personalizations": [{
                "dynamic_template_data": {
                    "domain": domain,
                    "digest": digest
                },
                "to": [{"email": email}]
            }],
            "from": {
                "name": self.config["sendgrid"]["from_name"],
                "email": self.config["sendgrid"]["from_email"]
            },
            "asm": {
                "group_id": int(self.config["sendgrid"]["asm_group_id"])
            }
        }
        r = requests.post("https://api.sendgrid.com/v3/mail/send",
                          headers=headers, json=payload)

        if r.status_code not in [200, 202]:
            print(r.status_code)
            print(r.text)