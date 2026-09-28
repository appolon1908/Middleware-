import {expect, it, vi} from 'vitest';
import {createJourneyService, normalizeJourney} from './journey';

it('passes explicit tenant, correlation, encoded identity, cursor and cancellation to the authenticated transport', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({channel_health:[]})));
  const controller = new AbortController();
  await createJourneyService('tenant-1', fetcher).journey('lead/a', 'cursor+a', controller.signal);
  expect(fetcher).toHaveBeenCalledWith('/platform/v1/leads/lead%2Fa/journey?limit=100&cursor=cursor%2Ba', {
    method:'GET',signal:controller.signal,headers:{Accept:'application/json','X-Tenant-ID':'tenant-1','X-Correlation-ID':expect.any(String)},
  });
});

it('surfaces canonical next-action errors', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({error:{message:'Authority unavailable'}}), {status:503}));
  await expect(createJourneyService('tenant-1', fetcher).nextAction('lead')).rejects.toThrow('Authority unavailable');
});

it('flattens suppressions nested in the authority channel health', () => {
  const suppression = {suppression_id:'s1',scope:'global',reason:'unsubscribe'};
  const journey = normalizeJourney({schema_version:'1.0',lead_id:'lead',lifecycle_state:'SUPPRESSED',lifecycle_version:2,transitions:[],exposures:[],next_cursor:null,
    channel_health:[
      {channel:'email',address_ref:'a',state:'unsubscribed',reason_code:'UNSUBSCRIBE',suppression},
      {channel:'sms',address_ref:'b',state:'suppressed',reason_code:'GLOBAL',suppression},
      {channel:'sms',address_ref:'c',state:'valid',reason_code:'OK',suppression:null},
    ]});
  expect(journey.suppressions).toEqual([{suppression_id:'s1',scope:'global',channel:null,reason:'unsubscribe'}]);
});
