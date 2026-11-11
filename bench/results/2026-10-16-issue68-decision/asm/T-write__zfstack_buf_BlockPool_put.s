   6edb0:	push   %rbp
   6edb1:	push   %r15
   6edb3:	push   %r14
   6edb5:	push   %r13
   6edb7:	push   %r12
   6edb9:	push   %rbx
   6edba:	sub    $0x128,%rsp
   6edc1:	mov    %rsi,%rbx
   6edc4:	mov    0xa8(%rdi),%rax
   6edcb:	test   %rax,%rax
   6edce:	je     6ede3 <zfstack::buf::BlockPool::put+0x33>
   6edd0:	cmp    0x30(%rbx),%rax
   6edd4:	jne    6ede3 <zfstack::buf::BlockPool::put+0x33>
   6edd6:	mov    0xb0(%rdi),%rax
   6eddd:	cmp    0x38(%rbx),%rax
   6ede1:	je     6ee10 <zfstack::buf::BlockPool::put+0x60>
   6ede3:	cmpq   $0x0,0x8(%rbx)
   6ede8:	je     6edf3 <zfstack::buf::BlockPool::put+0x43>
   6edea:	mov    (%rbx),%rdi
   6eded:	call   *0x4300d(%rip)        # b1e00 <free@GLIBC_2.2.5>
   6edf3:	add    $0x18,%rbx
   6edf7:	mov    %rbx,%rdi
   6edfa:	add    $0x128,%rsp
   6ee01:	pop    %rbx
   6ee02:	pop    %r12
   6ee04:	pop    %r13
   6ee06:	pop    %r14
   6ee08:	pop    %r15
   6ee0a:	pop    %rbp
   6ee0b:	jmp    6ec90 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6ee10:	mov    0x10(%rdi),%rcx
   6ee14:	mov    0x28(%rdi),%rax
   6ee18:	lea    (%rax,%rcx,1),%rdx
   6ee1c:	cmp    0xc8(%rdi),%rdx
   6ee23:	jne    6ef08 <zfstack::buf::BlockPool::put+0x158>
   6ee29:	cmpq   $0x800,0x8(%rbx)
   6ee31:	jne    6ee65 <zfstack::buf::BlockPool::put+0xb5>
   6ee33:	test   %rcx,%rcx
   6ee36:	je     6eed4 <zfstack::buf::BlockPool::put+0x124>
   6ee3c:	dec    %rcx
