   6d240:	push   %rbp
   6d241:	push   %r15
   6d243:	push   %r14
   6d245:	push   %r13
   6d247:	push   %r12
   6d249:	push   %rbx
   6d24a:	sub    $0x128,%rsp
   6d251:	mov    %rsi,%rbx
   6d254:	mov    0xa8(%rdi),%rax
   6d25b:	test   %rax,%rax
   6d25e:	je     6d273 <zfstack::buf::BlockPool::put+0x33>
   6d260:	cmp    0x30(%rbx),%rax
   6d264:	jne    6d273 <zfstack::buf::BlockPool::put+0x33>
   6d266:	mov    0xb0(%rdi),%rax
   6d26d:	cmp    0x38(%rbx),%rax
   6d271:	je     6d2a0 <zfstack::buf::BlockPool::put+0x60>
   6d273:	cmpq   $0x0,0x8(%rbx)
   6d278:	je     6d283 <zfstack::buf::BlockPool::put+0x43>
   6d27a:	mov    (%rbx),%rdi
   6d27d:	call   *0x43b7d(%rip)        # b0e00 <free@GLIBC_2.2.5>
   6d283:	add    $0x18,%rbx
   6d287:	mov    %rbx,%rdi
   6d28a:	add    $0x128,%rsp
   6d291:	pop    %rbx
   6d292:	pop    %r12
   6d294:	pop    %r13
   6d296:	pop    %r14
   6d298:	pop    %r15
   6d29a:	pop    %rbp
   6d29b:	jmp    6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d2a0:	mov    0x10(%rdi),%rcx
   6d2a4:	mov    0x28(%rdi),%rax
   6d2a8:	lea    (%rax,%rcx,1),%rdx
   6d2ac:	cmp    0xc8(%rdi),%rdx
   6d2b3:	jne    6d398 <zfstack::buf::BlockPool::put+0x158>
   6d2b9:	cmpq   $0x800,0x8(%rbx)
   6d2c1:	jne    6d2f5 <zfstack::buf::BlockPool::put+0xb5>
   6d2c3:	test   %rcx,%rcx
   6d2c6:	je     6d364 <zfstack::buf::BlockPool::put+0x124>
   6d2cc:	dec    %rcx
