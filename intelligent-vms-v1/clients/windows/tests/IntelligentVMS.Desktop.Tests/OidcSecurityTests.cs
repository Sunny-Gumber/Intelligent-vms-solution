using System.Net;
using System.Net.Http.Headers;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using Duende.IdentityModel.Client;
using Duende.IdentityModel.OidcClient;
using IntelligentVMS.Desktop;

internal static class OidcSecurityTests
{
    public static async Task RunAllAsync()
    {
        TestRfc7636S256Vector();
        await TestLibraryGeneratesFreshStateNonceAndS256Async();
        TestCallbackStateAndReplay();
        TestInvalidCallbackDoesNotConsumeAttempt();
        TestProfileCredentialOriginIsolation();
        await TestLoopbackListenerAsync();
        await TestLoopbackTimeoutAndCancellationAsync();
        TestCapabilityValidation();
        await TestApiAuthCapabilityIsUnauthenticatedAsync();
        await TestBoundedRefreshRetryAsync();
        await TestCredentialPurposeIsolationAsync();
        await TestCredentialCapacityFailureAsync();
        TestOidcSecretRedaction();
    }

    private static void TestRfc7636S256Vector()
    {
        const string verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
        const string expected="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
        Assert(OidcAuthorizationRequestValidator.S256(verifier)==expected);
    }

    private static async Task TestLibraryGeneratesFreshStateNonceAndS256Async()
    {
        var options=new OidcClientOptions
        {
            ProviderInformation=new ProviderInformation
            {
                IssuerName="https://identity.example.test/",
                AuthorizeEndpoint="https://identity.example.test/authorize",
                TokenEndpoint="https://identity.example.test/token",
            },
            ClientId="intelligent-vms-desktop",
            Scope="openid profile offline_access",
            RedirectUri="http://127.0.0.1:49152/oidc/callback/",
        };
        options.Policy.Discovery.RequireKeySet=false;
        var client=new OidcClient(options);
        var nonce1=RandomValue();
        var nonce2=RandomValue();
        var first=await client.PrepareLoginAsync(new Parameters{{"nonce",nonce1}});
        var second=await client.PrepareLoginAsync(new Parameters{{"nonce",nonce2}});
        Assert(!first.IsError&&!second.IsError);
        Assert(first.State!=second.State);
        Assert(first.CodeVerifier!=second.CodeVerifier);
        Assert(nonce1!=nonce2);
        OidcAuthorizationRequestValidator.ValidatePrepared(new Uri(first.StartUrl),options.RedirectUri,first.State,first.CodeVerifier,nonce1);
        OidcAuthorizationRequestValidator.ValidatePrepared(new Uri(second.StartUrl),options.RedirectUri,second.State,second.CodeVerifier,nonce2);
        var q=OidcAuthorizationRequestValidator.Query(new Uri(first.StartUrl));
        Assert(q["code_challenge_method"]=="S256");
        Assert(q["code_challenge"]==OidcAuthorizationRequestValidator.S256(first.CodeVerifier));
        Assert(!q.ContainsKey("client_secret"));
    }

    private static void TestCallbackStateAndReplay()
    {
        var gate=new OidcCallbackGate();
        var accepted=gate.ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=abc&state=state-1"),"state-1");
        Assert(accepted.Contains("code=abc",StringComparison.Ordinal));
        Throws<AuthenticationFlowException>(()=>gate.ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=again&state=state-1"),"state-1"));
        Throws<AuthenticationFlowException>(()=>new OidcCallbackGate().ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=abc&state=wrong"),"expected"));
        Throws<AuthenticationFlowException>(()=>new OidcCallbackGate().ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=abc"),"expected"));
        Throws<AuthenticationFlowException>(()=>new OidcCallbackGate().ValidateAndClaim(new Uri("http://127.0.0.1:45678/wrong/?code=abc&state=expected"),"expected"));
        Throws<AuthenticationFlowException>(()=>new OidcCallbackGate().ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?state=expected"),"expected"));
    }

    private static void TestInvalidCallbackDoesNotConsumeAttempt()
    {
        var gate=new OidcCallbackGate();
        Throws<AuthenticationFlowException>(()=>gate.ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=stale&state=wrong"),"expected"));
        var accepted=gate.ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=fresh&state=expected"),"expected");
        Assert(accepted.Contains("code=fresh",StringComparison.Ordinal));
        var ambiguous=new OidcCallbackGate();
        Throws<AuthenticationFlowException>(()=>ambiguous.ValidateAndClaim(new Uri("http://127.0.0.1:45678/oidc/callback/?code=abc&error=access_denied&state=expected"),"expected"));
    }

    private static void TestProfileCredentialOriginIsolation()
    {
        var id=Guid.NewGuid();
        var serverA=new ServerProfile(id,"A","https","vms-a.example.test",443);
        var renamedA=new ServerProfile(id,"Renamed","https","VMS-A.EXAMPLE.TEST",443);
        var serverB=new ServerProfile(id,"B","https","vms-b.example.test",443);
        var portChange=new ServerProfile(id,"A2","https","vms-a.example.test",8443);
        Assert(ServerProfileIdentity.SameCredentialOrigin(serverA,renamedA));
        Assert(!ServerProfileIdentity.SameCredentialOrigin(serverA,serverB));
        Assert(!ServerProfileIdentity.SameCredentialOrigin(serverA,portChange));
    }

    private static async Task TestLoopbackListenerAsync()
    {
        await using var listener=LoopbackCallbackListener.Create();
        var redirect=new Uri(listener.RedirectUri);
        Assert(redirect.Host=="127.0.0.1"&&IPAddress.IsLoopback(IPAddress.Parse(redirect.Host)));
        Assert(redirect.Port>0&&redirect.AbsolutePath=="/oidc/callback/");
        using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var wait=listener.WaitAsync("loopback-state",TimeSpan.FromSeconds(5),timeout.Token);
        using var http=new HttpClient();
        var response=await http.GetAsync(listener.RedirectUri+"?code=short-code&state=loopback-state",timeout.Token);
        var body=await response.Content.ReadAsStringAsync(timeout.Token);
        var callback=await wait;
        Assert(response.StatusCode==HttpStatusCode.OK);
        Assert(body.Contains("Authentication completed",StringComparison.Ordinal));
        Assert(!body.Contains("short-code",StringComparison.Ordinal));
        Assert(callback.Contains("code=short-code",StringComparison.Ordinal));
    }


    private static async Task TestLoopbackTimeoutAndCancellationAsync()
    {
        await using(var timeoutListener=LoopbackCallbackListener.Create())
        {
            try
            {
                await timeoutListener.WaitAsync("never-arrives",TimeSpan.FromMilliseconds(50),CancellationToken.None);
                throw new InvalidOperationException("Expected callback timeout.");
            }
            catch(AuthenticationFlowException ex){Assert(ex.Category=="callback_timeout");}
        }
        await using(var cancelListener=LoopbackCallbackListener.Create())
        {
            using var cancelled=new CancellationTokenSource();
            cancelled.Cancel();
            await ThrowsAsync<OperationCanceledException>(()=>cancelListener.WaitAsync("cancelled",TimeSpan.FromSeconds(5),cancelled.Token));
        }
    }

    private static void TestCapabilityValidation()
    {
        var valid=new OidcCapability{Enabled=true,Required=true,Authority="https://identity.example.test/",ClientId="desktop",Scopes=["openid","offline_access"],Callback="loopback",PkceMethods=["S256"]};
        OidcCapabilityValidator.Validate(valid);
        Throws<AuthenticationFlowException>(()=>OidcCapabilityValidator.Validate(withAuthority("http://identity.example.test/")));
        Throws<AuthenticationFlowException>(()=>OidcCapabilityValidator.Validate(withAuthority("https://identity.example.test/?tenant=other")));
        Throws<AuthenticationFlowException>(()=>OidcCapabilityValidator.Validate(new OidcCapability{Enabled=true,Authority="https://identity.example.test/",ClientId="desktop",Scopes=["profile"],Callback="loopback",PkceMethods=["S256"]}));
        Throws<AuthenticationFlowException>(()=>OidcCapabilityValidator.Validate(new OidcCapability{Enabled=true,Authority="https://identity.example.test/",ClientId="desktop",Scopes=["openid"],Callback="loopback",PkceMethods=["plain"]}));
        static OidcCapability withAuthority(string authority)=>new(){Enabled=true,Required=true,Authority=authority,ClientId="desktop",Scopes=["openid"],Callback="loopback",PkceMethods=["S256"]};
    }

    private static async Task TestApiAuthCapabilityIsUnauthenticatedAsync()
    {
        var handler=new CaptureHandler(_=>Json(HttpStatusCode.OK,"{\"authentication_required\":true,\"manual_token_login\":false,\"remember_session\":true,\"oidc\":{\"enabled\":true,\"required\":true,\"authority\":\"https://identity.example.test/\",\"client_id\":\"desktop\",\"scopes\":[\"openid\"],\"callback\":\"loopback\",\"pkce_methods\":[\"S256\"]}}"));
        using var api=new VmsApiClient(()=>null,handler);
        api.Configure(new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443));
        var caps=await api.GetAuthenticationCapabilitiesAsync();
        Assert(caps.Oidc.Enabled&&caps.Oidc.ClientId=="desktop");
        Assert(handler.Authorization is null);
        Assert(handler.Uri?.AbsolutePath=="/api/v1/auth/capabilities");
    }

    private static async Task TestBoundedRefreshRetryAsync()
    {
        var calls=0;var refreshes=0;var token="expired";
        var handler=new CaptureHandler(_=>{
            calls++;
            return calls==1?new HttpResponseMessage(HttpStatusCode.Unauthorized):Json(HttpStatusCode.OK,"{\"authenticated\":true,\"roles\":[\"viewer\"],\"tenant_id\":\"t\",\"site_ids\":[\"s\"]}");
        });
        using var api=new VmsApiClient(()=>token,handler,async _=>{refreshes++;token="renewed";await Task.Yield();return true;});
        api.Configure(new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443));
        var session=await api.GetSessionAsync();
        Assert(session.Authenticated&&calls==2&&refreshes==1&&handler.Authorization?.Parameter=="renewed");

        calls=0;refreshes=0;token="bad";
        var always401=new CaptureHandler(_=>{calls++;return new HttpResponseMessage(HttpStatusCode.Unauthorized);});
        using var api2=new VmsApiClient(()=>token,always401,async _=>{refreshes++;await Task.Yield();return true;});
        api2.Configure(new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443));
        await ThrowsAsync<SessionExpiredException>(()=>api2.GetSessionAsync());
        Assert(calls==2&&refreshes==1);
    }

    private static async Task TestCredentialPurposeIsolationAsync()
    {
        if(!OperatingSystem.IsWindows())return;
        var id=Guid.NewGuid();
        var manual=new WindowsCredentialStore();
        var oidc=new WindowsCredentialStore("oidc-refresh");
        try
        {
            await manual.SaveAsync(id,"manual-value");
            await oidc.SaveAsync(id,"refresh-value");
            Assert(await manual.LoadAsync(id)=="manual-value");
            Assert(await oidc.LoadAsync(id)=="refresh-value");
        }
        finally
        {
            await manual.DeleteAsync(id);
            await oidc.DeleteAsync(id);
        }
    }


    private static async Task TestCredentialCapacityFailureAsync()
    {
        if(!OperatingSystem.IsWindows())return;
        var store=new WindowsCredentialStore("oidc-refresh");
        var oversized=new string('x',1300);
        await ThrowsAsync<InvalidOperationException>(()=>store.SaveAsync(Guid.NewGuid(),oversized));
    }

    private static void TestOidcSecretRedaction()
    {
        var safe=SecretRedactor.Redact("code=secret-auth-code-481 code_verifier=secret-verifier-592 access_token=secret-access-603 refresh_token=secret-refresh-714 id_token=secret-identity-825 Authorization=secret-header-936");
        foreach(var secret in new[]{"secret-auth-code-481","secret-verifier-592","secret-access-603","secret-refresh-714","secret-identity-825","secret-header-936"})Assert(!safe.Contains(secret,StringComparison.Ordinal));
    }

    private static string RandomValue()=>OidcAuthorizationRequestValidator.Base64Url(RandomNumberGenerator.GetBytes(32));
    private static HttpResponseMessage Json(HttpStatusCode code,string json){var content=new StringContent(json,Encoding.UTF8);content.Headers.ContentType=new MediaTypeHeaderValue("application/json");return new HttpResponseMessage(code){Content=content};}
    private static void Assert(bool value,[CallerArgumentExpression("value")] string expression=""){if(!value)throw new InvalidOperationException($"OIDC test assertion failed: {expression}");}
    private static void Throws<T>(Action action) where T:Exception{try{action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}
    private static async Task ThrowsAsync<T>(Func<Task> action) where T:Exception{try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}

    private sealed class CaptureHandler(Func<HttpRequestMessage,HttpResponseMessage> responder):HttpMessageHandler
    {
        public AuthenticationHeaderValue? Authorization {get;private set;}
        public Uri? Uri {get;private set;}
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken)
        {
            Authorization=request.Headers.Authorization;Uri=request.RequestUri;
            return Task.FromResult(responder(request));
        }
    }
}
