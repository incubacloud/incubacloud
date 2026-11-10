# Mail relay

A production instance sends its email through a **mail relay**: a small mail
server next to Odoo that hands every message to your provider's SMTP server.
Odoo always sends to the relay; what changes is how the relay signs in to your
provider. You set it under **Instance detail → Networking**, and a **rebuild**
applies it.

The relay signs in in one of two ways:

- **Password**: a user and a password for your provider's SMTP server.
- **OAuth2 – Microsoft 365**: an app registered in Microsoft Entra ID, for a
  mailbox in Exchange Online. Microsoft turns SMTP sign-in with passwords off by
  default for Exchange Online at the end of December 2026, so a relay through
  `smtp.office365.com` with a password stops sending unless an administrator
  turns it on again.

A staging never uses the relay: its mail is captured on the server and shown in
its **Mails** tab.

## OAuth2 with Microsoft 365

The relay sends as one Microsoft 365 mailbox, the **sender mailbox**, through
`smtp.office365.com`, port 587, with STARTTLS. It signs in with the client
credentials of an app you register in your Microsoft 365 organisation. You need
an administrator who can register apps in Entra ID and run Exchange Online
PowerShell.

### 1. Register the app in Entra ID

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), open
   **App registrations → New registration**. Give it a name (for example
   *Odoo mail relay*), keep **Single tenant** and register it.
2. On the app's **Overview**, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. Open **API permissions → Add a permission → APIs my organization uses**,
   search for **Office 365 Exchange Online**, choose **Application permissions**
   and tick **SMTP.SendAsApp**. Then **Grant admin consent**.
4. Open **Certificates & secrets → New client secret**. Copy its **Value** (not
   its *Secret ID*) at once: Entra ID shows it only now. A secret lasts at most
   24 months; note when it expires.

### 2. Give the app the mailbox in Exchange Online

Connect with `Connect-ExchangeOnline` as an administrator, then:

1. Register the app's service principal. Use the **Object ID** shown under
   **Enterprise applications** (search for the app's name there), not the one
   under App registrations: the wrong one makes the sign-in fail.

    ```powershell
    New-ServicePrincipal -AppId <Application (client) ID> -ObjectId <Object ID from Enterprise applications>
    ```

2. Give it the sender mailbox:

    ```powershell
    Add-MailboxPermission -Identity notifications@example.com -User <Object ID from Enterprise applications> -AccessRights FullAccess
    ```

3. Turn SMTP sign-in on for that mailbox. OAuth2 needs it on too:

    ```powershell
    Set-CASMailbox -Identity notifications@example.com -SmtpClientAuthenticationDisabled $false
    ```

    If SMTP sign-in is off for the whole organisation, turn it on there as well
    (`Set-TransportConfig -SmtpClientAuthenticationDisabled $false`).

4. Turn off Entra ID's **security defaults** (Entra admin center → Overview →
   Properties → Manage security defaults) if they are on: while they are on,
   Microsoft refuses SMTP sign-in with `535 5.7.139` whatever the settings
   above say. An organisation with Conditional Access manages this through its
   policies instead. The change can take a few minutes to apply.

Give the app this one mailbox only: whoever holds its secret can send as it.

### 3. Fill in the relay

Under **Instance detail → Networking**:

1. **SMTP Authentication**: *OAuth2 – Microsoft 365*. The host, port and
   encryption are set for you.
2. **Sender mailbox**: the mailbox of step 2.
3. **Directory ID** and **Application (client) ID**: the two IDs of step 1.
4. **Client secret**: the secret's value. Once saved, it is never shown again.
5. **Save**, then **Rebuild** the instance.

Odoo's outgoing mail must be sent as the sender mailbox. To send as another
address, give the mailbox *Send As* on it
(`Add-RecipientPermission -Identity sales@example.com -Trustee notifications@example.com -AccessRights SendAs`);
without it Microsoft refuses the mail with `5.2.252 SendAsDenied`.

The password you had is kept: switching back to **Password** finds it.

## Send test mail

Under the relay's fields, **Send test mail** sends one mail to your own address
through the relay, and the job says whether your provider's server took it, with
its answer. It is available once the instance is deployed, running and its
changes are applied, five times an hour. A mail that did not leave is removed
from the relay's queue; one that did also sends at once the mail that was
waiting. When the server only defers the mail for a reason the panel does not
recognise, the job gives no verdict and the regular checks decide.

## When the relay does not deliver

The panel checks every production relay every few minutes. When it cannot hand
mail to your provider's server for two checks in a row, counting only mail the
relay has already tried again, it raises a critical
alert, **smtp_relay_failing**, with the cause and what to do. Mail is not lost
meanwhile: it waits in the relay's queue for up to 5 days and is sent as soon
as the relay can; after that it goes back to its sender. A server refusing one
recipient (a full or unknown mailbox) is not a relay failure and raises nothing.

The alert closes only when mail goes out again: a test mail that was sent, or
any mail the relay delivers after the last failure the panel saw.

| What the alert says | What to do |
|---|---|
| SMTP AUTH is disabled (`535 5.7.139`) | Turn SMTP sign-in on for the mailbox and the organisation, and turn off security defaults (step 2). |
| The server refuses the app's token (`535 5.7.3`) | Register the service principal with the Object ID from Enterprise applications, and give it FullAccess on the mailbox (step 2). |
| Entra ID refuses the client secret | Copy the secret's **Value**, not its *Secret ID*, into the relay. |
| The client secret has expired | Create a new secret in Entra ID, save it in the relay and rebuild. |
| Entra ID does not find the application or the directory | Check both IDs on the app's Overview. |
| The server refuses the sender (`5.2.252 SendAsDenied`) | Send from the mailbox, or give it Send As on the other address. |
| Microsoft no longer takes passwords | Switch the relay to OAuth2 – Microsoft 365. |
| The server refuses the connection, does not answer, or does not resolve | Check the SMTP host and port, and that the server's firewall lets the instance's server out on that port. |
| TLS fails, or the server offers no STARTTLS | Check the port and the encryption. |
| The relay's content filter (Amavis) does not take the mail | Restart the instance; if it goes on, rebuild it. |

Exchange Online accepts 30 messages a minute and 10,000 recipients a day per
mailbox; above that it defers the mail, which the relay sends again later.
