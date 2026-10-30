# Meta channel setup

The Channels page can publish to Facebook Pages, Instagram professional
accounts, and Threads. Each connection uses a Meta access token entered as a
secret in the admin console. Tokens are stored in the private announcements
database, are never shown again, and are not refreshed automatically. Use a
long-lived token and reconnect with a renewed token when Meta expires it.

## Facebook Pages

1. Create a Meta app with the Pages API enabled and sign in as an admin of the
   Page.
2. Use Meta's Graph API Explorer to grant `pages_show_list`,
   `pages_read_engagement`, and `pages_manage_posts`, then select the Page and
   generate its Page access token.
3. In **Admin → Channels → Facebook Page**, enter the numeric Page ID and the
   Page access token. A personal user token is not a substitute.
4. Use **Test connection** before enabling publication.

Text posts and public image posts are supported. Text can be edited in place
and posts can be withdrawn. Replacing an image on an already published post is
not supported.

## Instagram

1. Use an Instagram Business or Creator account. Create a Meta app, add the
   Instagram API using Instagram Login, and configure its Business Login
   settings.
2. Authorize the account with `instagram_business_basic` and
   `instagram_business_content_publish`, then exchange the authorization code
   for a long-lived Instagram user access token.
3. In **Admin → Channels → Instagram professional account**, enter the
   numeric professional account ID and the access token.
4. Use **Test connection** before enabling publication.

Instagram publishing requires an image on every announcement, served from the
public announcement feed URL. Published posts cannot be edited or withdrawn
through this API.

## Threads

1. Create a Threads app and authorize the account that should publish.
2. Request `threads_basic` and `threads_content_publish`. Also request
   `threads_delete` if withdrawal should remove published posts.
3. Exchange the authorization code for a long-lived Threads user access token.
   Enter the numeric Threads user ID and access token in **Admin → Channels →
   Threads**.
4. Use **Test connection** before enabling publication.

Threads supports text and public images. Posts cannot be edited. Withdrawal
requires the `threads_delete` permission. Long-lived tokens expire and must be
renewed manually.

Official references:

- [Facebook Pages API posts](https://developers.facebook.com/documentation/pages-api/posts)
- [Instagram Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- [Business Login for Instagram](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)
- [Threads API setup](https://developers.facebook.com/documentation/threads/get-started)
- [Threads access tokens and permissions](https://developers.facebook.com/documentation/threads/get-started/get-access-tokens-and-permissions)
